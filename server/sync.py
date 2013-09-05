from __future__ import division
import sys, os;  sys.path.insert(1, os.path.abspath(os.path.join(os.path.dirname( __file__ ), '..')))

import sessionmanager
from models import db_session, FolderMeta, UIDValidity, MessageMeta, MessagePart
from sqlalchemy import distinct, func
import sqlalchemy.exc

from encoding import EncodingError
from server.util import chunk, partition
import logging as log

def refresh_crispin(email):
    return sessionmanager.get_crispin_from_email(email)

def load_validity_cache(crispin_client, email):
    # in practice UIDVALIDITY and HIGHESTMODSEQ are always positive
    # integers with gmail, but let's not take chances on our default.
    defaults = dict(UIDVALIDITY=float('-inf'), HIGHESTMODSEQ=float('-inf'))
    # populated cache looks like:
    # {'Inbox': {'UIDVALIDITY': 123, 'HIGHESTMODSEQ': 456}}
    cache_validity = dict([(folder, defaults.copy())
        for folder in crispin_client.sync_folders])
    for folder, uid_validity, highestmodseq in db_session.query(
            UIDValidity.folder_name,
            UIDValidity.uid_validity,
            UIDValidity.highestmodseq).filter_by(
                    g_email=email, folder_name=folder):
        cache_validity[folder] = dict(UIDVALIDITY=uid_validity,
                HIGHESTMODSEQ=highestmodseq)

    return cache_validity

def check_uidvalidity(crispin_client):
    if not uidvalidity_valid(crispin_client):
        log.info("UIDVALIDITY for {0} has changed; resyncing UIDs".format(
            crispin_client.selected_folder_name))
        resync_uids(crispin_client)

def uidvalidity_valid(crispin_client):
    """ Validate UIDVALIDITY on currently selected folder. """
    try:
        cached_validity = db_session.query(UIDValidity.uid_validity).filter_by(
                g_email=crispin_client.email_address,
                folder_name=crispin_client.selected_folder_name).one()[0]
    except sqlalchemy.orm.exc.NoResultFound:
        # No entry? No problem!
        return True
    assert type(cached_validity) == type(crispin_client.selected_uidvalidity)
    return crispin_client.selected_uidvalidity >= cached_validity

def resync_uids(crispin_client):
    """ Call this when UIDVALIDITY is invalid to fix up the database.

    What happens here is we fetch new UIDs from the IMAP server and match
    them with X-GM-MSGIDs and sub in the new UIDs for the old. No messages
    are re-downloaded.
    """
    raise Exception("Unimplemented")

def delete_messages(uids, folder):
    # delete these UIDs from this folder
    fm_query = db_session.query(FolderMeta).filter(
            FolderMeta.msg_uid.in_(uids),
            FolderMeta.folder_name==folder)
    # g_msgids = [fm.g_msgid for fm in fm_query]
    fm_query.delete(synchronize_session='fetch')

    # XXX TODO not sure if there's a good non-expensive way to find
    # dangling messages; we may want to have a different worker deal
    # with this
    # if the g_msgid is now dangling, delete the message meta and parts as
    # well
    # dangling_g_msgids = [g_msgid for g_msgid, count in
    # db_session.query(FolderMeta.g_msgid,
    #     func.count(FolderMeta)).group_by(FolderMeta.g_msgid)\
    #         .filter(FolderMeta.g_msgid.in_(g_msgids)) if count == 0]
    # #
    # db_session.query(MessageMeta).filter(
    #         MessageMeta.g_msgids.in_(dangling_g_msgids)).delete()
    # db_session.query(MessagePart).filter(
    #         MessagePart.g_msgids.in_(dangling_g_msgids)).delete()
    # XXX also delete message parts from the block store
    db_session.commit()

def remove_deleted_messages(crispin_client):
    """ Works as follows:
        1. do a LIST on the current folder to see what messages are on the server
        2. compare to message uids stored locally
        3. purge messages we have locally but not on the server. ignore
            messages we have on the server that aren't local.
    """
    server_uids = crispin_client.all_uids()
    local_uids = [uid for uid, in
            db_session.query(FolderMeta.msg_uid).filter_by(
                folder_name=crispin_client.selected_folder_name)]
    if len(server_uids) > 0 and len(local_uids) > 0:
        assert type(server_uids[0]) == type(local_uids[0])

    to_delete = set(local_uids).difference(set(server_uids))
    if to_delete:
        delete_messages(to_delete, crispin_client.selected_folder_name)

def new_or_updated(uids, folder):
    local_uids = set([unicode(uid) for uid, in \
            db_session.query(FolderMeta.msg_uid).filter(
            FolderMeta.folder_name==folder,
            FolderMeta.msg_uid.in_(uids))])
    return partition(lambda x: x not in local_uids, uids)

def incremental_sync(user_email_address):
    """ Poll this every N seconds for active (logged-in) users and every
        N minutes for logged-out users. It checks for changed message metadata
        and new messages using CONDSTORE / HIGHESTMODSEQ and also checks for
        deleted messages.

        We may also wish to frob update frequencies based on which folder
        a user has visible in the UI as well.
    """
    crispin_client = refresh_crispin(user_email_address)
    cache_validity = load_validity_cache(crispin_client, user_email_address)
    needs_update = []
    for folder in crispin_client.sync_folders:
        # eventually we might want to be holding a cache of this stuff from any
        # SELECT calls that have already happened, to save on a status call.
        # but status is fast, so maybe not.
        status = crispin_client.imap_server.folder_status(folder,
                ('UIDVALIDITY', 'HIGHESTMODSEQ'))
        cached_highestmodseq = cache_validity[folder]['HIGHESTMODSEQ']
        if status['HIGHESTMODSEQ'] > cached_highestmodseq:
            needs_update.append((folder, cached_highestmodseq))

    for folder, highestmodseq in needs_update:
        highestmodseq_update(folder, crispin_client)

    return 0

def update_cached_highestmodseq(folder, crispin_client, cached_validity=None):
    if cached_validity is None:
        cached_validity = db_session.query(UIDValidity).filter_by(
                g_email=crispin_client.email_address, folder_name=folder).one()
    cached_validity.highestmodseq = crispin_client.selected_highestmodseq
    db_session.add(cached_validity)

def highestmodseq_update(folder, crispin_client, cached_validity=None):
    crispin_client.select_folder(folder)
    check_uidvalidity(crispin_client)
    uids = crispin_client.imap_server.search(
            ['NOT DELETED', 'MODSEQ {0}'.format(
                crispin_client.selected_highestmodseq)])
    log.info("Starting highestmodseq update on {0} (current HIGHESTMODSEQ: {1})".format(folder, crispin_client.selected_highestmodseq))
    if uids:
        new, updated = new_or_updated(uids, folder)
        for uids in chunk(new, crispin_client.CHUNK_SIZE):
            new_messagemeta, new_messagepart, new_foldermeta = safe_download(new,
                    folder, crispin_client)
            db_session.add_all(new_foldermeta)
            db_session.add_all(new_messagemeta)
            db_session.add_all(new_messagepart)
            safe_commit()
        # bigger chunk because the data being fetched here is very small
        for uids in chunk(updated, 5*crispin_client.CHUNK_SIZE):
            update_metadata(updated, crispin_client)
            safe_commit()
    remove_deleted_messages(crispin_client)
    # not sure if this one is actually needed - does delete() automatically
    # commit?
    safe_commit()

    update_cached_highestmodseq(folder, crispin_client, cached_validity)
    db_session.commit()

def safe_download(uids, folder, crispin_client):
    try:
        new_messagemeta, new_messagepart, new_foldermeta = \
                crispin_client.fetch_uids(uids)
    except EncodingError, e:
        raise
    # XXX make this catch more specific
    except Exception, e:
        log.error("Crispin fetch failure: %s. Reconnecting..." % e)
        crispin_client = refresh_crispin(crispin_client.email_address)
        new_messagemeta, new_messagepart, new_foldermeta = \
                crispin_client.fetch_uids(uids)

    return new_messagemeta, new_messagepart, new_foldermeta

def safe_commit():
    try:
        db_session.commit()
    except sqlalchemy.exc.SQLAlchemyError, e:
        log.error(e.orig.args)
    except Exception, e:
        log.error("Unknown exception: %s" % e)

def update_metadata(uids, crispin_client):
    """ Update flags (the only metadata that can change). """
    new_metadata = crispin_client.fetch_metadata(uids)
    for fm in db_session.query(FolderMeta).filter(
            FolderMeta.msg_uid.in_(uids),
            FolderMeta.folder_name==crispin_client.selected_folder_name):
        if fm.flags != new_metadata[fm.uid]:
            fm.flags = new_metadata[fm.uid]
            db_session.add(fm)

def initial_sync(user_email_address):
    """ Downloads entire messages and
    (1) creates the metadata database
    (2) stores message parts to the block store
    """
    crispin_client = refresh_crispin(user_email_address)

    log.info('Syncing mail for {0}'.format(user_email_address))

    # message download for messages from sync_folders is prioritized before
    # AllMail in the order of appearance in this list

    for folder in crispin_client.sync_folders:
        # for each folder, compare what's on the server to what we have.
        # this allows restarts of the initial sync script in the case of
        # total failure.
        crispin_client.select_folder(folder)
        check_uidvalidity(crispin_client)
        server_uids = crispin_client.all_uids()
        server_g_msgids = crispin_client.fetch_g_msgids(server_uids)
        g_msgids = set([g_msgid for g_msgid, in
            db_session.query(distinct(FolderMeta.g_msgid))])

        log.info("Found {0} UIDs for folder {1}".format(
            len(server_uids), folder))
        existing_uids = [uid for uid, in
                db_session.query(FolderMeta.msg_uid).filter_by(
                    g_email=user_email_address, folder_name=folder)]
        log.info("Already have {0} items".format(len(existing_uids)))
        warn_uids = set(existing_uids).difference(set(server_uids))
        unknown_uids = set(server_uids).difference(set(existing_uids))

        if warn_uids:
            delete_messages(warn_uids, folder)
            log.info("Deleted the following UIDs that no longer exist on the server: {0}".format(' '.join(sorted(warn_uids, key=int))))

        full_download, foldermeta_only = partition(
                lambda uid: server_g_msgids[uid] in g_msgids,
                sorted(unknown_uids, key=int))

        log.info("{0} uids left to fetch".format(len(full_download)))

        log.info("skipping {0} uids downloaded via other folders".format(
            len(foldermeta_only)))
        if len(foldermeta_only) > 0:
            db_session.add_all(
                    [crispin_client.make_fm(server_g_msgids[uid], folder,
                        uid) for uid in foldermeta_only])
            db_session.commit()

        total_messages = len(existing_uids)

        log.info("Starting sync for {0} with chunks of size {1}".format(
            folder, crispin_client.CHUNK_SIZE))
        for uids in chunk(full_download, crispin_client.CHUNK_SIZE):
            new_messagemeta, new_messagepart, new_foldermeta = \
                    safe_download(uids, folder, crispin_client)
            db_session.add_all(new_foldermeta)
            db_session.add_all(new_messagemeta)
            db_session.add_all(new_messagepart)

            safe_commit()

            total_messages += len(uids)

            log.info("Synced %i of %i (%.4f%%)" % (total_messages,
                                                   len(server_uids),
                                                    total_messages / len(server_uids) * 100))

        # transaction commit
        try:
            cached_validity = db_session.query(UIDValidity).filter_by(
                    g_email=user_email_address, folder_name=folder).one()
            if cached_validity.highestmodseq < crispin_client.selected_highestmodseq:
                # if we've done a restart on the initial sync, we may have already
                # saved a UIDValidity row for any given folder, so we need to
                # update it instead. BUT, we first need to check for updated
                # metadata since the recorded HIGHESTMODSEQ. (Yes, some messages
                # here may have already been downloaded in the UID query above;
                # the modseq update will properly skip those messages that already
                # exist locally.)
                highestmodseq_update(folder, crispin_client, cached_validity)
            else:
                # nothing to do here
                pass
        except sqlalchemy.orm.exc.NoResultFound:
            db_session.add(UIDValidity(
                g_email=user_email_address, folder_name=folder,
                uid_validity=crispin_client.selected_uidvalidity,
                highestmodseq=crispin_client.selected_highestmodseq))
            db_session.commit()
        log.info("Saved all messages and metadata on {0} to UIDVALIDITY {1} / HIGHESTMODSEQ {2}".format(folder, crispin_client.selected_uidvalidity,
            crispin_client.selected_highestmodseq))

    log.info("Finished.")

    crispin_client.user_obj.initial_sync_done = True
    db_session.add(crispin_client.user_obj)
    db_session.commit()

    return 0
