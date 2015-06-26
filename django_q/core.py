# Future
from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
import ast
from builtins import dict
from builtins import range

from future import standard_library

standard_library.install_aliases()

# Standard
import importlib
import logging
import os
import signal
from multiprocessing import Queue, Event, Process, current_process
import socket
import sys
from time import sleep

try:
    import cPickle as pickle
except ImportError:
    import pickle

# External
import redis
import arrow

# Django
from django.core import signing
from django.utils import timezone

# Local
from .conf import LOG_LEVEL, SECRET_KEY, SAVE_LIMIT, WORKERS, COMPRESSED, PREFIX
from .humanhash import uuid
from .models import Task, Success, Schedule

SIGNAL_NAMES = dict((getattr(signal, n), n) for n in dir(signal) if n.startswith('SIG') and '_' not in n)

logger = logging.getLogger('django-q')


# Optional coloredlogs support
try:
    import coloredlogs

    coloredlogs.install(level=getattr(logging, LOG_LEVEL))
except ImportError:
    coloredlogs = None

# Set up standard logging handler
if not logger.handlers:
    logger.setLevel(level=getattr(logging, LOG_LEVEL))

    formatter = logging.Formatter(fmt='%(asctime)s [django_q] %(message)s',
                                  datefmt='%H:%M:%S')
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logger.addHandler(handler)

Q_LIST = '{}:q'.format(PREFIX)
STARTING = 'Starting'
RUNNING = 'Running'
STOPPED = 'Stopped'
STOPPING = 'Stopping'

r = redis.StrictRedis()


class Cluster(object):
    def __init__(self, list_key=Q_LIST):
        try:
            r.ping()
        except ():
            logger.error('Can not connect to Redis server')
            return
        self.sentinel = None
        self.stop_event = None
        self.start_event = None
        self.stopped_event = None
        self.pid = current_process().pid
        self.host = socket.gethostname()
        self.list_key = list_key
        signal.signal(signal.SIGTERM, self.sig_handler)
        signal.signal(signal.SIGINT, self.sig_handler)

    def start(self):
        # This is just for PyCharm to not crash. Ignore it.
        if not hasattr(sys.stdin, 'close'):
            def dummy_close():
                pass

            sys.stdin.close = dummy_close
        # Start Sentinel
        self.stop_event = Event()
        self.start_event = Event()
        self.sentinel = Process(target=Sentinel, args=(self.stop_event, self.start_event, self.list_key))
        self.sentinel.start()
        logger.info('Q Cluster-{} starting.'.format(self.pid))
        while not self.start_event.is_set():
            sleep(0.2)
        return self.pid

    def stop(self):
        if not self.sentinel.is_alive():
            return False
        logger.info('Q Cluster-{} stopping.'.format(self.pid))
        self.stop_event.set()
        self.sentinel.join()
        logger.info('Q Cluster-{} has stopped.'.format(self.pid))
        self.start_event = None
        self.stop_event = None
        return True

    def sig_handler(self, signum, frame):
        logger.debug('{} got signal {}'.format(current_process().name, SIGNAL_NAMES.get(signum, 'UNKNOWN')))
        self.stop()

    @property
    def stat(self):
        if self.sentinel:
            return Stat.get(self.pid)
        return Status(self.pid)

    @property
    def is_starting(self):
        return self.stop_event and self.start_event and not self.start_event.is_set()

    @property
    def is_running(self):
        return self.stop_event and self.start_event and self.start_event.is_set()

    @property
    def is_stopping(self):
        return self.stop_event and self.start_event and self.start_event.is_set() and self.stop_event.is_set()

    @property
    def has_stopped(self):
        return self.start_event is None and self.stop_event is None and self.sentinel

    @property
    def is_idle(self):
        return self.sentinel is None


class Sentinel(object):
    def __init__(self, stop_event, start_event, list_key=Q_LIST, start=True):
        # Make sure we catch signals for the pool
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        self.pid = current_process().pid
        self.parent_pid = os.getppid()
        self.name = current_process().name
        self.list_key = list_key
        self.status = None
        self.reincarnations = 0
        self.tob = timezone.now()
        self.stop_event = stop_event
        self.start_event = start_event
        self.pool_size = WORKERS
        self.pool = []
        self.task_queue = Queue()
        self.done_queue = Queue()
        self.event_out = Event()
        self.monitor_pid = None
        self.pusher_pid = None
        if start:
            self.spawn_cluster()
            self.guard()

    def spawn_process(self, target, *args):
        # This is just for PyCharm to not crash. Ignore it.
        if not hasattr(sys.stdin, 'close'):
            def dummy_close():
                pass

            sys.stdin.close = dummy_close
        p = Process(target=target, args=args)
        p.daemon = True
        self.pool.append(p)
        p.start()
        return p.pid

    def spawn_pusher(self):
        return self.spawn_process(pusher, self.task_queue, self.event_out, self.list_key)

    def spawn_worker(self):
        self.spawn_process(worker, self.task_queue, self.done_queue)

    def spawn_monitor(self):
        return self.spawn_process(monitor, self.done_queue)

    def reincarnate(self, pid):
        if pid == self.monitor_pid:
            self.spawn_monitor()
            logger.warn("reincarnated monitor after death of {}".format(pid))
        elif pid == self.pusher_pid:
            self.spawn_pusher()
            logger.warn("reincarnated pusher after death of {}".format(pid))
        else:
            self.spawn_worker()
            logger.warn("reincarnated work worker after death of {}".format(pid))
        self.reincarnations += 1

    def spawn_cluster(self):
        self.set_status(STARTING)
        for i in range(self.pool_size):
            self.spawn_worker()
        self.monitor_pid = self.spawn_monitor()
        self.pusher_pid = self.spawn_pusher()

    def guard(self):
        logger.info('{} guarding cluster at {}'.format(current_process().name, self.pid))
        self.start_event.set()
        self.set_status(RUNNING)
        logger.info('Q Cluster-{} running.'.format(self.parent_pid))
        counter = 0
        while True:
            for p in list(self.pool):
                if not p.is_alive():
                    p.terminate()
                    self.pool.remove(p)
                    self.reincarnate(p.pid)
            Stat(self).save()
            if self.stop_event.is_set():
                break
            # Call scheduler once a minute (or so)
            counter += 1
            if counter > 30:
                counter = 0
                scheduler()
            sleep(2)
        self.stop()

    def stop(self):
        self.set_status(STOPPING)
        name = current_process().name
        logger.info('{} stopping pool processes'.format(name))
        # Stopping pusher
        self.event_out.set()
        # Putting poison pills in the queue
        for _ in self.pool:
            self.task_queue.put('STOP')
        while len(self.pool) > 2:
            for p in list(self.pool):
                if not p.is_alive():
                    logger.debug('{} stopped gracefully'.format(p.pid))
                    self.pool.remove(p)
            sleep(0.2)
        # Finally stop the monitor
        self.done_queue.put('STOP')
        self.pool = []
        self.set_status(STOPPED)

    def set_status(self, message=None):
        Stat(self, message).save()


def pusher(task_queue, e, list_key=Q_LIST):
    logger.info('{} pushing tasks at {}'.format(current_process().name, current_process().pid))
    while True:
        task = r.blpop(list_key, 1)
        if task:
            task = task[1]
            task_queue.put(task)
            logger.debug('queueing {}'.format(task))
        if e.is_set():
            break
    logger.info("{} stopped pushing tasks".format(current_process().name))


def monitor(done_queue):
    name = current_process().name
    logger.info("{} monitoring at {}".format(name, current_process().pid))
    for task in iter(done_queue.get, 'STOP'):
        if task['success']:
            logger.info("Processed [{}]".format(task['name']))
        else:
            logger.error("Failed [{}] - {}".format(task['name'], task['result']))
        save_task(task)
    logger.info("{} stopped monitoring results".format(name))


def worker(task_queue, done_queue):
    name = current_process().name
    logger.info('{} ready for work at {}'.format(name, current_process().pid))
    task = {}

    def return_pack(res, success):
        task['result'] = res
        task['stopped'] = timezone.now()
        task['success'] = success
        done_queue.put(task)

    for pack in iter(task_queue.get, 'STOP'):
        # unpickle the task
        try:
            task = SignedPackage.loads(pack)
        except TypeError as e:
            logger.error(e)
            continue
        except signing.BadSignature as e:
            task['name'] = task['name'].rsplit(":", 1)[0]
            return_pack(e, False)
            continue
        logger.info('{} processing [{}]'.format(name, task['name']))
        f = task['func']
        # if it's not an instance try to get it from the string
        if not callable(task['func']):
            try:
                module, func = f.rsplit('.', 1)
                m = importlib.import_module(module)
                f = getattr(m, func)
            except (ValueError, ImportError, AttributeError) as e:
                logger.error(e)
                return_pack(e, False)
                continue
        # execute the payload
        try:
            result = f(*task['args'], **task['kwargs'])
            return_pack(result, True)
        except Exception as e:
            return_pack(e, False)
    logger.info('{} stopped doing work'.format(name))


def save_task(task):
    """
    Saves the task package to Django
    """
    if task['success'] and 0 < SAVE_LIMIT < Success.objects.count():
        Success.objects.first().delete()
    Task.objects.create(name=task['name'],
                        func=task['func'],
                        hook=task['hook'],
                        args=task['args'],
                        kwargs=task['kwargs'],
                        started=task['started'],
                        stopped=task['stopped'],
                        result=task['result'],
                        success=task['success'])


def async(func, *args, **kwargs):
    """
    Schedules a task with optional hook
    """
    # Check for hook
    if 'hook' in kwargs:
        hook = kwargs['hook']
        del kwargs['hook']
    else:
        hook = None
    # Check for list_key override
    if 'list_key' in kwargs:
        list_key = kwargs['list_key']
        del kwargs['list_key']
    else:
        list_key = Q_LIST
    task = {'name': uuid()[0], 'func': func, 'hook': hook, 'args': args, 'kwargs': kwargs, 'started': timezone.now()}
    pack = SignedPackage.dumps(task)
    r.rpush(list_key, pack)
    logger.debug('Pushed {}'.format(pack))
    return task['name']


class SignedPackage(object):
    """
    Wraps Django's signing module with custom Pickle serializer
    """

    @staticmethod
    def dumps(obj, compressed=COMPRESSED):
        return signing.dumps(obj,
                             key=SECRET_KEY,
                             salt='django_q.q',
                             compress=compressed,
                             serializer=PickleSerializer)

    @staticmethod
    def loads(obj):
        return signing.loads(obj,
                             key=SECRET_KEY,
                             salt='django_q.q',
                             serializer=PickleSerializer)


class PickleSerializer(object):
    """
    Simple wrapper around Pickle for signing.dumps and
    signing.loads.
    """

    @staticmethod
    def dumps(obj):
        return pickle.dumps(obj)

    @staticmethod
    def loads(data):
        return pickle.loads(data)


class Status(object):
    def __init__(self, pid):
        self.workers = []
        self.tob = None
        self.reincarnations = 0
        self.cluster_id = pid
        self.sentinel = 0
        self.status = 'Idle'
        self.done_q_size = 0
        self.host = socket.gethostname()
        self.monitor = 0
        self.task_q_size = 0
        self.pusher = 0
        self.timestamp = timezone.now()


class Stat(Status):
    def __init__(self, sentinel, message=None):
        super(Stat, self).__init__(sentinel.parent_pid)
        if message:
            sentinel.status = message
        self.tob = sentinel.tob
        self.reincarnations = sentinel.reincarnations
        self.sentinel = sentinel.pid
        self.status = sentinel.status
        self.done_q_size = sentinel.done_queue.qsize()
        self.monitor = sentinel.monitor_pid
        self.task_q_size = sentinel.task_queue.qsize()
        self.pusher = sentinel.pusher_pid
        for w in sentinel.pool:
            self.workers.append(w.pid)

    def uptime(self):
        return (timezone.now() - self.tob).total_seconds()

    @property
    def key(self):
        return self.get_key(self.cluster_id)

    @staticmethod
    def get_key(cluster_id):
        return '{}:cluster:{}'.format(PREFIX, cluster_id)

    def save(self):
        r.set(self.key, SignedPackage.dumps(self, True), 3)

    def empty_queues(self):
        return self.done_q_size + self.task_q_size == 0

    @staticmethod
    def get(cluster_id):
        key = Stat.get_key(cluster_id)
        if r.exists(key):
            pack = r.get(key)
            try:
                return SignedPackage.loads(pack)
            except signing.BadSignature:
                return None
        return Status(cluster_id)

    @staticmethod
    def get_all():
        stats = []
        keys = r.keys(pattern='{}:cluster:*'.format(PREFIX))
        if keys:
            packs = r.mget(keys)
            for pack in packs:
                try:
                    stats.append(SignedPackage.loads(pack))
                except signing.BadSignature:
                    continue
        return stats


def scheduler():
    for schedule in Schedule.objects.exclude(repeats=0).filter(next_run__lt=timezone.now()):
        args = ()
        kwargs = {}
        # get args, kwargs and hook
        if schedule.kwargs:
            try:
                # eval should be safe here cause dict()
                kwargs = eval('dict({})'.format(schedule.kwargs))
            except SyntaxError:
                kwargs = {}
        if schedule.args:
            args = ast.literal_eval(schedule.args)
            # single value won't eval to tuple, so:
            if type(args) != tuple:
                args = (args,)
        if schedule.hook:
            kwargs['hook'] = schedule.hook
        # set up the next run time
        if not schedule.schedule_type == schedule.ONCE:
            next_run = arrow.get(schedule.next_run)
            if schedule.schedule_type == schedule.HOURLY:
                next_run = next_run.replace(hours=+1)
            elif schedule.schedule_type == schedule.DAILY:
                next_run = next_run.replace(days=+1)
            elif schedule.schedule_type == schedule.WEEKLY:
                next_run = next_run.replace(weeks=+1)
            elif schedule.schedule_type == schedule.MONTHLY:
                next_run = next_run.replace(months=+1)
            elif schedule.schedule_type == schedule.QUARTERLY:
                next_run = next_run.replace(months=+3)
            elif schedule.schedule_type == schedule.YEARLY:
                next_run = next_run.replace(years=+1)
            schedule.next_run = next_run.datetime
            schedule.repeats += -1
        else:
            schedule.repeats = 0
        # send it to the cluster
        schedule.task = async(schedule.func, *args, **kwargs)
        if not schedule.task:
            logger.error('{} failed to create task from  schedule {}').format(current_process().name, schedule.id)
        else:
            logger.info('{} created [{}] from schedule {}'.format(current_process().name, schedule.task, schedule.id))
        schedule.save()