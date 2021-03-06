import collections
import datetime
import itertools

from bson import objectid
from pymongo import errors
from pymongo import ReturnDocument

from anubis import error
from anubis import db
from anubis.constant import contest
from anubis.util import argmethod
from anubis.util import validator
from anubis.util import json
from anubis.model import system
from anubis.model import user
from anubis.model import record
from anubis.service import bus

RULE_OI = 2
RULE_ACM = 3

TYPE_ONLINE = 1
TYPE_OFFLINE = 2

RULE_TEXTS = {
    RULE_OI: 'OI',
    RULE_ACM: 'ACM-ICPC',
}

Rule = collections.namedtuple('Rule', ['show_func', 'stat_func', 'status_sort', 'rank_func'])


def _oi_stat(tdoc, journal):
    detail = list(dict((j['pid'], j) for j in journal if j['pid'] in tdoc['pids']).values())
    return {'score': sum(d['score'] for d in detail), 'detail': detail}


def _acm_stat(tdoc, journal):
    naccept = collections.defaultdict(int)
    effective = {}
    for j in journal:
        if j['pid'] in tdoc['pids'] and not (j['pid'] in effective and effective[j['pid']]['accept']):
            effective[j['pid']] = j
            if not j['accept']:
                naccept[j['pid']] += 1

    def time(jdoc):
        real = jdoc['rid'].generation_time.replace(tzinfo=None) - tdoc['begin_at']
        penalty = datetime.timedelta(minutes=20) * naccept[jdoc['pid']]
        return (real + penalty).total_seconds()

    detail = [{**j, 'naccept': naccept[j['pid']], 'time': time(j)} for j in effective.values()]
    return {'accept': sum(int(d['accept']) for d in detail),
            'time': sum(d['time'] for d in detail if d['accept']),
            'detail': detail}


def _acm_rank(tsdocs):
    now = 1
    gold = contest.COUNT_GOLD
    silver = gold + contest.COUNT_SILVER
    bronze = silver + contest.COUNT_BRONZE
    for tsdoc in tsdocs:
        prize = None
        if now <= gold:
            prize = 'gold'
        elif gold < now <= silver:
            prize = 'silver'
        elif silver < now <= bronze:
            prize = 'bronze'
        if prize:
            tsdoc['prize'] = prize
        ranked = tsdoc.get('ranked', True)
        if ranked:
            rank = now
            now += 1
        else:
            rank = '*'
        yield (rank, tsdoc)


RULES = {
    RULE_ACM: Rule(lambda tdoc, now: now >= tdoc['begin_at'],
                   _acm_stat, [('accept', -1), ('time', 1)], _acm_rank),
}


def convert_to_pid(pids: list, pid_letter: str):
    try:
        return pids[ord(pid_letter) - ord('A')]
    except IndexError:
        raise error.ContestProblemNotFoundError(pid_letter)


def convert_to_letter(pids: list, pid: int):
    try:
        return chr(pids.index(pid) + ord('A'))
    except ValueError:
        raise error.ContestProblemNotFoundError(pid)


@argmethod.wrap
async def add(domain_id: str, title: str, content: str, owner_uid: int, rule: int, private: bool,
              begin_at: lambda i: datetime.datetime.utcfromtimestamp(int(i)),
              end_at: lambda i: datetime.datetime.utcfromtimestamp(int(i)),
              pids=[], **kwargs):
    validator.check_title(title)
    validator.check_content(content)
    if rule not in RULES:
        raise error.ValidationError('rule')
    if begin_at >= end_at:
        raise error.ValidationError('begin_at', 'end_at')
    # TODO: should we check problem existance here?
    tid = await system.inc_contest_counter()
    coll = db.Collection('contest')
    doc = {
        '_id': tid,
        'domain_id': domain_id,
        'title': title,
        'content': content,
        'owner_uid': owner_uid,
        'rule': rule,
        'private': private,
        'begin_at': begin_at,
        'end_at': end_at,
        'pids': pids,
        'attend': 0,
        **kwargs,
    }
    await coll.insert_one(doc)
    return tid


@argmethod.wrap
async def edit(domain_id: str, tid: int, **kwargs):
    if 'title' in kwargs:
        validator.check_title(kwargs['title'])
    if 'content' in kwargs:
        validator.check_content(kwargs['content'])
    coll = db.Collection('contest')
    tdoc = await coll.find_one_and_update(filter={'domain_id': domain_id,
                                                  '_id': tid},
                                          update={'$set': kwargs},
                                          return_document=True)
    if not tdoc:
        raise error.ContestNotFoundError(domain_id, tid)
    return tdoc


@argmethod.wrap
async def get(domain_id: str, tid: int):
    coll = db.Collection('contest')
    tdoc = await coll.find_one({'domain_id': domain_id, '_id': tid})
    if not tdoc:
        raise error.ContestNotFoundError(domain_id, tid)
    return tdoc


def get_multi(domain_id: str, projection=None, **kwargs):
    coll = db.Collection('contest')
    return coll.find({'domain_id': domain_id, **kwargs}, projection=projection)


@argmethod.wrap
async def get_list(domain_id: str, projection=None):
    return await get_multi(domain_id=domain_id,
                           projection=projection).sort([('_id', -1)]).to_list(None)


@argmethod.wrap
async def attend(domain_id: str, tid: int, uid: int):
    #  TODO: check time.
    coll = db.Collection('contest.status')
    try:
        await coll.find_one_and_update(filter={'domain_id': domain_id,
                                               'tid': tid,
                                               'uid': uid,
                                               'attend': {'$eq': 0}},
                                       update={'$set': {'attend': 1}},
                                       upsert=True,
                                       return_document=ReturnDocument.AFTER)
    except errors.DuplicateKeyError:
        raise error.ContestAlreadyAttendedError(domain_id, tid, uid) from None
    coll = db.Collection('contest')
    return await coll.find_one_and_update(filter={'domain_id': domain_id,
                                                  '_id': tid},
                                          update={'$inc': {'attend': 1}},
                                          return_document=ReturnDocument.AFTER)


@argmethod.wrap
async def remove_status(domain_id: str, tid: int, uid: int):
    tsdoc = await get_status(domain_id, tid, uid)
    if not tsdoc:
        raise error.UserNotFoundError(uid)
    for j in tsdoc['journal']:
        await record.remove_property(j['rid'], 'tid')
    coll = db.Collection('contest.status')
    await coll.delete_one({'domain_id': domain_id,
                           'tid': tid,
                           'uid': uid})
    return tsdoc


@argmethod.wrap
async def get_status(domain_id: str, tid: int, uid: int, projection=None):
    coll = db.Collection('contest.status')
    return await coll.find_one({'domain_id': domain_id, 'tid': tid, 'uid': uid},
                               projection=projection)


def get_multi_status(*, projection=None, **kwargs):
    coll = db.Collection('contest.status')
    return coll.find(kwargs, projection=projection)


async def get_dict_status(domain_id, uid, tids, *, projection=None):
    result = dict()
    async for tsdoc in get_multi_status(domain_id=domain_id,
                                        uid=uid,
                                        tid={'$in': list(set(tids))},
                                        projection=projection):
        result[tsdoc['tid']] = tsdoc
    return result


@argmethod.wrap
async def get_and_list_status(domain_id: str, tid: int, projection=None):
    # TODO: projection, pagination
    tdoc = await get(domain_id, tid)
    tsdocs = await get_multi_status(domain_id=domain_id,
                                    tid=tid,
                                    projection=projection
                                    ).sort(RULES[tdoc['rule']].status_sort).to_list(None)
    return tdoc, tsdocs


@argmethod.wrap
async def update_status(domain_id: str, tid: int, uid: int, rid: objectid.ObjectId,
                        pid: int, accept: bool):
    tdoc = await get(domain_id, tid)
    if pid not in tdoc['pids']:
        raise error.ValidationError('pid')

    coll = db.Collection('contest.status')
    tsdoc = await coll.find_one_and_update(filter={'domain_id': domain_id,
                                                   'tid': tid,
                                                   'uid': uid},
                                           update={
                                               '$push': {
                                                   'journal': {'rid': rid,
                                                               'pid': pid,
                                                               'accept': accept}
                                               },
                                               '$inc': {'rev': 1}},
                                           return_document=ReturnDocument.AFTER)
    if not tsdoc:
        return {}
    if 'attend' not in tsdoc or not tsdoc['attend']:
        raise error.ContestNotAttendedError(domain_id, tid, uid)

    # Sort and uniquify journal of the contest status, by rid.

    key_func = lambda j: j['rid']
    journal = [list(g)[-1]
               for _, g in itertools.groupby(sorted(tsdoc['journal'], key=key_func), key=key_func)]
    stats = RULES[tdoc['rule']].stat_func(tdoc, journal)
    psdict = {}
    for detail in tsdoc.get('detail', []):
        psdict[detail['pid']] = detail
    for detail in stats.get('detail', []):
        detail['balloon'] = psdict.get(detail['pid'], {'balloon': False}).get('balloon', False)
    tsdoc = await coll.find_one_and_update(filter={'domain_id': domain_id,
                                                   'tid': tid,
                                                   'uid': uid},
                                           update={'$set': {'journal': journal, **stats},
                                                   '$inc': {'rev': 1}},
                                           return_document=ReturnDocument.AFTER)
    await bus.publish('contest_notification-' + str(tid), json.encode({'type': 'rank_changed'}))
    if accept and not psdict.get(pid, {'accept': False})['accept']:
        await set_status_balloon(domain_id, tid, uid, pid, False)
    return tsdoc


@argmethod.wrap
async def set_status_balloon(domain_id: str, tid: int, uid: int, pid: int, balloon: bool=True):
    tdoc = await get(domain_id, tid)
    if pid not in tdoc['pids']:
        raise error.ValidationError('pid')

    coll = db.Collection('contest.status')
    tsdoc = await coll.find_one_and_update(filter={'domain_id': domain_id,
                                                   'tid': tid,
                                                   'uid': uid,
                                                   'detail.pid': pid},
                                           update={'$set': {'detail.$.balloon': balloon}},
                                           return_document=ReturnDocument.AFTER)
    udoc = await user.get_by_uid(uid)
    await bus.publish('balloon_change', json.encode({'uid': uid,
                                                     'uname': udoc['uname'],
                                                     'nickname': udoc.get('nickname', ''),
                                                     'tid': tid,
                                                     'pid': pid,
                                                     'letter': convert_to_letter(tdoc['pids'], pid),
                                                     'balloon': balloon}))
    return tsdoc


@argmethod.wrap
async def create_indexes():
    coll = db.Collection('contest')
    await coll.create_index([('domain_id', 1),
                             ('_id', 1)], unique=True)
    await coll.create_index([('domain_id', 1),
                             ('pids', 1)], sparse=True)
    await coll.create_index([('domain_id', 1),
                             ('rule', 1),
                             ('_id', -1)], sparse=True)
    status_coll = db.Collection('contest.status')
    await status_coll.create_index([('domain_id', 1),
                                    ('uid', 1),
                                    ('tid', 1)], unique=True)
    await status_coll.create_index([('domain_id', 1),
                                    ('tid', 1),
                                    ('accept', -1),
                                    ('time', 1)], sparse=True)
    await status_coll.create_index([('domain_id', 1),
                                    ('tid', 1),
                                    ('detail.accept', 1),
                                    ('detail.balloon', -1)])
    await status_coll.create_index([('domain_id', 1),
                                    ('tid', 1),
                                    ('uid', 1),
                                    ('detail.pid', 1)], sparse=True)


if __name__ == '__main__':
    argmethod.invoke_by_args()

