"""
Tasks reported stats:
* task - task name
* event - what have happened
* value:
    * for event "started" means duration between received and started - that is wait in queue
    * for "retried", "succeeded" and "failed" - execution times

"""
import gevent.monkey

gevent.monkey.patch_all()


import logging
import os

from celery import Celery
import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration
from broker import Redis
from gevent import Greenlet
from influx import TaskStats, QueueStats, WorkerStats
from expiringdict import ExpiringDict

debug = os.environ.get('DEBUG')
logging.basicConfig(level=logging.DEBUG if debug else logging.INFO)
log = logging.getLogger(__name__)

freq = float(os.environ.get('FREQUENCY', 10))
BROKER_URL = os.environ.get('CELERY_BROKER_URL')
celery = Celery(broker=BROKER_URL)
redis = Redis(BROKER_URL)

sentry_dsn = os.environ.get('SENTRY_DSN')
if sentry_dsn:
    sentry_sdk.init(
        dsn=sentry_dsn,
        integrations=[LoggingIntegration(level=logging.INFO, event_level=logging.ERROR)],
        debug=debug,
    )

heartbeats = set()

# We don't receive event when task is ignored or revoked, thus there will always be ghost uids in the system that
# never go away. We'll purge them after a week
tuids = ExpiringDict(max_len=10240, max_age_seconds=60 * 60 * 24 * 7)


def event_dispatcher(event):
    handlers = {
        'task-succeeded': task_handler,
        'task-sent': task_handler,
        'task-received': task_handler,
        'task-started': task_handler,
        'task-failed': task_handler,
        'task-rejected': task_handler,
        'task-revoked': task_handler,
        'task-retried': task_handler,
        'worker-heartbeat': worker_heartbeat,
        'worker-online': worker_heartbeat,
    }
    func = handlers.get(event['type'], None)
    if func:
        gevent.spawn(func, event)
    else:
        log.debug(f'Received event with no handler: {event["type"]}')


def worker_heartbeat(event):
    global heartbeats
    heartbeats.add(event['hostname'])


def task_handler(event):
    try:
        cname = event['type'][5:]
        uuid = event['uuid']
        duration = None

        if cname == 'received':
            name = event['name']
            tuids.update({uuid: {'name': name, 'received': event['timestamp'], 'started': 0}})
        elif cname == 'started':
            if uuid not in tuids:
                return
            name = tuids[uuid]['name']
            tuids[uuid]['started'] = event['timestamp']

            duration = tuids[uuid]['started'] - tuids[uuid]['received']
        else:
            if uuid not in tuids:
                return
            name = tuids[uuid]['name']

        if not name:
            log.error(f'Event for task with no name: {event}')
            return

        if cname in ['succeeded', 'failed', 'retried', 'rejected', 'revoked']:

            if tuids[uuid]['started']:
                duration = event['timestamp'] - tuids[uuid]['started']
            del tuids[uuid]

        log.debug(f'[{len(tuids)}]  {name} [{cname}] = {duration}')
        TaskStats(
            task=name,
            event=cname,
            duration=duration or 0.,
        )
    except Exception as ex:
        log.exception(str(ex), event)


class Collector(Greenlet):
    def _run(self):
        log.debug(f'Started collecting from {BROKER_URL}')
        while True:
            try:
                with celery.connection() as connection:
                    recv = celery.events.Receiver(
                        connection,
                        handlers={'*': event_dispatcher},
                    )
                    recv.capture(limit=None, timeout=None, wakeup=True)
            except (KeyboardInterrupt, SystemExit):
                return
            except Exception as ex:
                log.exception('Unhandled Collector exception')


class Submitter(Greenlet):
    def _run(self):
        global heartbeats
        while True:
            try:
                gevent.sleep(freq)

                for name, count in redis.itercounts():
                    name = str(name)
                    log.debug(f'Report queue: {name} = {count}')
                    QueueStats(queue=name, count=count)

                WorkerStats(count=len(heartbeats))
                log.debug(f'Report {len(heartbeats)} workers')

                heartbeats = set()

                try:
                    QueueStats.commit()
                except AttributeError:  # Raised when there were no events committed to the stats collector
                    pass

                try:
                    WorkerStats.commit()
                except AttributeError:  # Raised when there were no events committed to the stats collector
                    pass
            except (KeyboardInterrupt, SystemExit):
                return
            except Exception as ex:
                log.exception('Unhandled Submitter exception')


if __name__ == '__main__':
    s = Submitter()
    s.start()

    g = Collector()
    g.start()

    try:
        gevent.joinall([s, g])
    except (KeyboardInterrupt, SystemExit):
        pass
