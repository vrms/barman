# What does each phase actually look like and mean and stuff?

## Remove `xlog.db` and replace with storage metadata

### Survey of existing `xlog.db` stuff

#### Implementation

The implementation of `xlog.db` is mostly in `barman.server.Server` with logic to rebuild `xlog.db` being found in `barman.backup.BackupManager`.

Functions:

* `barman.backup.BackupManager.rebuild_xlogdb`
* `barman.server.Server.xlogdb_file_name`
* `barman.server.Server.xlogdb`
* `barman.infofile.WalFileInfo.to_xlogdb_line`
* `barman.infofile.WalFileInfo.from_xlogdb_line`

#### Clients

Clients of `xlog.db` are:

* `barman.backup.BackupManager.remove_wal_before_backup`
  * gets the dirname of the WALs
  * scans xlogdb:
    * creates WalFileInfo
    * only actually uses the WAL name
* `barman.server.Server.check_archive`
  * literally just checking if it's empty or not
* `barman.server.Server.get_required_xlog_files`
  * scans xlogdb:
    * creates WalFileInfo
    * uses wal_info.name and wal_into.time
    * also yields wal_info to whatever called it
      * which is _xlog_copy, which uses name and compression
* `barman.server.Server.get_wal_until_next_backup`
  * scans xlogdb
    * creates WalFileInfo
    * uses wal_info.name
    * yields to get_wal_info and get_list_of_files
* `barman.server.Server.sync_status`
  * scans xlogdb
    * creates WalFileInfo
    * uses wal_info.name
* `barman.server.Server.sync_wals`
  * writes new shit to xlog.db
* `barman.server.Server.set_sync_starting_point`
  * used by sync_status to find where to start

#### Details

We write to `xlog.db` in three places:

* `barman.backup.BackupManager.rebuild_xlogdb`
* `barman.wal_archiverWalArchiver.archive_wal`
* `barman.server.Server.sync_wals`

The format of an `xlog.db` record is:

    name\tsize\ttime\tcompression

When we read `xlog.db` it is always a sequential scan through the file.

When we write we are always appending lines to the file with the exception of the `barman.backup.BackupManager.rebuild_xlogdb` function which swaps in a new `xlog.db` file.

### Requirements for Storage Metadata v1

The high level requirements are:

1. Be able to find the location of a WAL across storage tiers.
2. Be able to return the WalFileInfo for that WAL.
3. Be able to scan through all WALs and return an iterable of WalFileInfo objects.

`xlog.db` is currently protected by a lock so it doesn't really matter how we implement it - the important thing initially is to standardize the code so that `xlog.db` is completely abstracted and Barman only deals with Storage Metadata and WalFileInfo objects.

So we could do this in two parts:

1. Abstract away `xlog.db` behind Storage Metadata.
2. Re-implement `xlog.db` with some fancy new thing - maybe that's an embedded SQL DB, maybe a KV store.

The first part is the tricky bit really because we need to define a suitable interface, refactor the code and update the tests.

### Concerns

The integration tests rely on being able to just write lines to `xlog.db` in order to reset Barman to a happy state where it can take a backup. How will they do this now?

All access to `xlog.db` uses locking. Sometimes this is because it's a file and we don't want multiple things updating it at the same time. Other times, however, the lock is because we are actually archiving the WAL and we want this to be atomic wrt `xlog.db`. We therefore keep the lock but move it so it's not a server concern.

Is Storage Metadata even the right thing for `xlog.db`?

We'll probably also need to transparently fall through to the original `xlog.db` so we need an implementation which has the exact same format as the original for cases where no one wants to use the tiered storage. The implementation can be a little different but the file on disk must be the same.

Probably have separate locks for backup metadata and xlog metadata.

Sync status stuff needs rewriting... I think we keep `last_position` and it's just up to storage_metadata to interpret that.
For a file implementation it's the line to start from.
The important thing is that `last_position` values can't be mixed - e.g. if migrating from old to new storage model.

Rebuild xlogdb? We still need to be able to do this.

### Hack 1

#### Minimal...

* Replace `barman.server.Server.xlogdb` with Metadata
* Replace these with logic in Metadata: `barman.infofile.WalFileInfo.to_xlogdb_line`, `barman.infofile.WalFileInfo.from_xlogdb_line`
* Remove this completely, hopefully: `barman.server.Server.xlogdb_file_name`
* Implement `barman.backup.BackupManager.rebuild_xlogdb` using Metadata

Then update the following to just interact with Metadata:

* ~`barman.backup.BackupManager.remove_wal_before_backup`
  - here we need to handle the fact that we write to a new file and swap it in
  - let's just rewrite this
    - except this "delete all WALs before backup..." logic is going to need to be aware of tiers and probably use the backup metadata too... maybe we'll leave it as it is for now
* ~`barman.server.Server.check_archive`
* ~`barman.server.Server.get_required_xlog_files`
* ~`barman.server.Server.get_wal_until_next_backup`
* `barman.server.Server.sync_status`
* `barman.server.Server.sync_wals`
* `barman.server.Server.set_sync_starting_point`
* ~`barman.wal_archiver.WalArchiver.archive_wal`

* re-implement so we're keeping the file open and holding seek positions
  etc - we will need to do this for backward compatibility
    - or can we ditch backward compatibility at this point?
* remove remaining xlogdb-isms (e.g. from_xlog_line etc)
* rename metadata to xlog_metadata
  - also it is logically different to the backup metadata so it should
    just be a separate thing (it may happen to share the same backend
    like a SQLite or LevelDB or whatever, but code interacting with
    xlog metadata should not be using the same abstractions as code
    interacting with backup metadata)

#### Better

Move all wal-finding logic into Metadata