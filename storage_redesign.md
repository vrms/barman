# Barman Storage Redesign Take 2

## Purpose of this document

This document describes the architectural and high level design aspects of the barman storage redesign.
The idea is to be a concise explanation of the proposed design, the trade-offs and the open questions.

## Overview

Firstly, we're adding a feature and at the same time refactoring the internal storage to meet our own maintenance requirements.
We therefore need to be clear about what the feature is, i.e. how will users be exposed to this change?

The key questions are:

1. What should the feature look like to the user?
2. How do we implement this internally?
  a. What components do we need to add to barman?
  b. What existing bits of barman need updating and how?

## Motivation

### Feature

From a feature perspective we want to simplify the way users have to implement their backup architectures and reduce the amount of moving parts that they need to deal with.
Instead of installing barman and barman-cloud and potentially connecting things with hook scripts, they can achieve their goals by adding a combination of retention policies and backup placement directives.
The user states where backups need to be and how long they should be kept for, barman does the rest.

There is a second feature which is the transparent processing of backups.
This would allow users to specify what kind of post-processing they want (compression at first, encryption and `custom` later) and how many backups they want to keep in a non-processed state for quick recovery.
`barman cron` would then determine whether backups need processing and apply the processing.

### Refactoring

From a maintainers perspective the goal is to bring our storage models together so that we no longer have to maintain two similar-but-different bits of code.
This is particularly desirable because the code in question is business-critical - mistakes here can be the kind that take us on a one way trip to data loss central.
We really only want to have to worry about getting it right once, not twice.

We therefore introduce the concept of tiered storage into barman - there would be a local tier and an off-site tier, with some configuration and logic which determines which backups need to be placed in which tier.
The processing feature would be implemented internally as another storage tier, or perhaps as part of the local tier.

## The problem space

We want to expose as few storage details as possible while allowing users enough flexibility to achieve their business goals.
Given the diverse and bespoke nature of Enterprise backup architectures we should be aiming to meet the most common scenarios which would cover around 80% of use cases out there.

Most disaster recovery plans will be based around the concepts of Recovery Point Objective (RPO) and Recovery Time Objective (RTO).
Specifically this means the maximum amount of data loss allowed (usually expressed as a time window) and the maximum allowable time to recover to the point at which the RPO is met.

RPO and RTO are typically constraints of the business and it is the job of the IT practitioner (Ops, DevOps, DevSecOps or even... Sysadmin) to make sure they can be met by implementing a backup/restore strategy.
This might look something like this:

1. Regular backups kept on-site.
2. Regular, but less frequent, backups which are kept off-site.
3. Because we're talking about PostgreSQL specifically: WAL shipping.

The frequency of the on-site backups combined with the availability of WALs determines the RPO and RTO.
There are also options available to achieve reduced RPO including:

* Zero RPO through synchronous replication - this probably falls into the 20% of use cases (the rarer ones) due to the performance implications of synchronously replicating writes.
* Asynchronous replication.

The off-site backups exist to provide redundancy such that DR is still possible even in the event of total destruction of the primary location.
A higher RTO and RPO are generally assumed for off-site backups though this may not always be the case.
Depending on how WALs are shipped it is possible to achieve a low RPO even with off-site backups, though the RTO is still likely to be higher due to the increased amount of WAL replay and the need to transfer the WALs back on site.

### Example scenarios

#### Daily backups, weekly off-site backups

* A full backup is made once per day and kept on the local barman server.
* WAL shipping is set up such that WALs are archived on the local barman server.
* Once per week, one of the daily backups is transferred off-site along with all WALs required for the consistency of that backup.
* Daily backups are kept for 14 days.
* The weekly off-site backups are kept for 2 months.

This yields an RPO roughly equivalent to the value of `archive_timeout` and an RTO of `time to restore backup` + `time to replay WALs` for the local backups.
For the off-site backup the maximum RPO is around a week though the RTO will be reasonably low since all that is needed is to restore the backup and replay any WALs saved with the backup.

#### Daily backups, latest backup on-site all others off-site

* A full backup is made once per day and kept on the local barman server.
* WAL shipping is set up such that WALs are archived on the local barman server.
* Whenever a new backup is taken the previous backup is transferred off site along with all WALs needed for PITR to the present.
* The off-site backups are kept for 14 days.

In this scenario a single "hot" backup is kept on site and whenever a new one is taken the older one is transferred off site.
This would be desirable in a situation where storage on the barman server itself is limited (w.r.t the backup size - it could still be a big disk...) but there is plenty of off-site storage available either in the form of cheap cloud storage or a backup solution or a SAN etc.

## High level choices / trade-offs

### Preserve existing functionality

If a user makes no config changes they should be able to upgrade to the tiered-storage Barman and everything should work as usual.
There may be some differences in the output (for example some show/list commands may include tier information) but everything should continue to work as normal even though behind the scenes things may be a little different.

### Backups cannot be duplicated across tiers

A backup can only exist in one tier at a time.
We will not make it possible to have multiple copies of the same backup in different tiers.
This is so that we can keep things reasonably simple, with one `server/backup_id` representing a unique backup in the system which could be located on any tier.
Barman will need to make accessing the backup a transparent operation such that the user can see the tier in which it is located but does not have to do anything tier-specific to interact with it.

If actual redundant copies of backups are required then barman's geo-redundancy feature is available to meet that need.

Note: The actual mechanics of moving a backup between tiers will be copy-then-delete however as far as the metadata is concerned a single backup only exists in one tier.

### WALs can and will be duplicated across tiers

WALs are copied so that each tier contains all the WALs necessary to recover a backup in that tier to the current point in time.
If we were to *move* WALs along with backups then we would require all tiers to be available in order to perform a full PITR from a given backup on any tier.
This is not great from an availability point of view - we would up-to-triple our chances of not being able to fully recover.

Optional: We could consider using `full` or `standalone` in the metadata for all backups so that we know whether we need WALs or not.
If all off-site backups are `standalone`, for example, then there is no need to copy all the WALs from the local tier.
Only those WALs necessary to recover the `standalone` backups.
The presence of a `full` backup would require copying all WALs from the `end_wal` of that backup.

### WAL storage/retrieval is independent of backups

As with current retention policies, we determine which WALs are required in a location depending on the backups which are present.
Then we copy them from another tier.
A deletion phase which follows the duplication phase will identify any that can be removed.

Barman will need to have a way of retrieving WALs for a backup during a restore.

### Backup metadata is stored in a centralized location

Barman will have a centralized metadata store which contains existing `backup.info` data for each unique backup along with placement information and any annotations.
This will be used by existing code which currently parses `backup.info` files (retention policies and many more).
This will also be used by new Barman components required to deal with tiers and backup placement.

The behavior of building up the backup catalog at runtime by scanning the available `backup.info` files will be retired.

This has the advantage of allowing Barman to work with off-site tiers which do not allow immediate access to the backup files such as S3 glacier.

Optional: We may still wish to include the `backup.info` file with the backup and a "repair" mode such that in the event of losing the centralized metadata store we can recreate it, or at least enough of it to allow backups to continue and restores to be possible.
This presents its own challenges because metadata may change over time.

Optional: We may want to automatically copy the metadata to the off-site tier so that we have some built-in protection against losing the metadata.

### Backups are stored as a single opaque binary object

Once a backup has been processed it will be treated like an opaque binary object.
This is possible because backup metadata will be maintained in a centralized catalog and therefore we do not need to be able to access the `backup.info` file, or any annotation files, directly.

The raw/hot tier will therefore export a backup as a single tarball which can be received by the processing tier and compressed/encrypted as desired - the default behavior will be that it stores an uncompressed tarball containing all backup files (the pgdata directory, any tablespaces and the `backup.info` file).

The raw/hot tier itself will continue to store backups in their current on-disk format.

### Backups are moved between tiers as file-like objects

The file-like object is already the de-facto standard for transferring backups to and from cloud storage.
We will continue to use this approach for moving backups between the raw/hot tier and the processed tier.
Ideally we will also use this approach for moving backups to other forms of off-site storage such as backup solutions but this depends on the backup solution APIs.

All implementations of backup transfer will need to stream the data such that we avoid storing the entirety of the backup in RAM.

### Backup placement is automated

Users will specify their backup placement requirements but Barman itself will make decisions as to where backups should be at any given time and when to move them.

Optionally: We *could* allow users to have manual control over movement of backups between tiers.
This would involve adding commands such as `barman move-backup <SRC_TIER> <DST_TIER>` and users would be free to implement their own tier movement policies using cron.
This might be a nice power-user feature but does not meet the user-facing goals of this work - we want to provide a simple way of achieving common backup architectures and exposing tier movement to users directly does not achieve this.

### The backup placement algorithm is subject to change

The implementation of backup placement, and the associated parameters which are exposed to users, is currently still being investigated and may be subject to change in the future.
Tier storage and recovery must therefore be completely independent of the algorithm used to place backups into tiers.
Tiers should have a well defined interface for backup movement and should not need to know anything about how Barman decides the location of a backup.

### How to determine backup placements?

Backup placement will be determined by an algorithm which is applied by `barman cron`.
The inputs will be the user-controllable parameters (set through configuration) and the current backup locations.

User-controllable parameters will be:

* minimum_raw_backups = N
  * The minimum number of backups which should be in the raw state, i.e. immediately available for recovery.
* offsite_transfer = N {BACKUPS | DAYS | WEEKS}
  * The frequency at which backups should be transferred off site.
* retention_policy = {REDUNDANCY value | RECOVERY WINDOW OF value {DAYS | WEEKS | MONTHS}}}
* offsite_retention_policy = {REDUNDANCY value | RECOVERY WINDOW OF value {DAYS | WEEKS | MONTHS}}}

Backup placement will be determined as follows:

* If there are > `minimum_raw_backups` backups in the raw tier then the oldest backup is moved to the processed tier.
* If the offsite_transfer interval has been exceeded then the newest backup in the processed tier is moved to the off-site tier.
* The WALs needed for a full PITR from the backups in the processed and off-site tiers are determined and copied.
* The local and off-site retention policies are applied (these trigger a WAL cleanup on the tier in question).

This is a simple scheme which exposes minimal parameters to the user and yields predictable behavior.
It is not necessarily the *optimal* scheme but should be good enough for most use-cases.

The two examples discussed earlier could be achieved as follows:

* Daily backups, weekly off-site backups
  * minimum_raw_backups = 1
  * offsite_transfer = 7 DAYS
  * retention_policy = RECOVERY WINDOW OF 14 DAYS
  * offsite_retention_policy = RECOVERY WINDOW OF 2 MONTHS

* Daily backups, latest backup on-site all others off-site
  * minimum_raw_backups = 1
  * offsite_transfer = 1 DAY
  * retention_policy = REDUNDANCY 2
  * offsite_retention_policy = RECOVERY WINDOW OF 14 DAYS

Note that the retention policy is only considered when retention policies are applied, not when backups are moved.
This means that the amount of backups which will actually be present in the processed and off-site tiers is derived (from the backup frequency and offsite transfer frequency) rather than specified directly.

### How to implement retention policies across tiers?

Currently we are considering the following possibilities:

* A single, global, retention policy:
  * The retention policy is applied regardless of where the backups are actually located.
* Per-tier retention policies:
  * Raw, processed and off-site tiers all get their own retention policy.
* Local / off-site retention policies:
  * The raw and processed tiers are combined under one retention policy and off-site has its own.
* Processed and off-site retention policies:
  * The raw tier does not get a retention policy - it just gets the "minimum backups" and that's it, and the processed and off-site tiers have their own individual retention policies.

The last option is possibly easier to implement and works with the parameters we're already considering exposing.

### Restore

Restore will initially be achieved by either:

* Direct restore from the raw/hot tier.
* Staging a processed or off-site backup onto the Barman store and restoring from the staging location.

This simplifies the changes required to the restore logic however the cost is that the user must have enough spare disk space on the barman server to be able to stage the backup.

Optional: Direct restore from processed and off-site tiers.
This would be achieved in a similar manner to the existing barman-cloud restore function which streams the restored backup directly to disk on the barman server.
We would require some component to be available on the PostgreSQL server being restored.

## Barman internals

To make this happen we need to do some reasonably disruptive work in the barman internals, adding some new components and modifying existing components to yield the desired behavior.

### New components

#### Storage Metadata

Metadata for all backups including everything currently stored in `backup.info` files, any backup annotations and everything currently served by `xlog.db`.
This could be a simple filesystem-backed storage or an embedded database of some sort.
The rest of the Barman code should not be exposed to the storage implementation.

Consumers of the Storage Metadata will include anything which needs to know where backups are located so this includes:

* show/list backup functions
* retention policy logic

#### Storage Manager

The storage manager is the barman component which knows about the storage tiers and allows other parts of barman to access the backups.

Consumers include:

* `barman cron`, which will need to apply the backup placement scheme and apply any necessary backup movements
* recovery logic, which will need to stage and restore a backup
* WAL retrieval logic

#### Storage Tiers

A storage tier is responsible for knowing all the implementation details of that tier, the important ones being able to store backups, know how to find them and retrieve them when required.
It will have methods for ingress, egress, deletion, checking existence and restoring.
The parameters and return values for the methods should be the same across tiers though practical concerns may mean some tiers need to deviate.

### Existing components

This work is going to touch a lot of barman.
Anything which currently interacts with the backup catalog, `xlog.db`, `backup.info` files and potentially anything which interacts with backups on disk (code which writes the initial backup is mostly excepted since this is just considered writing directly into the raw tier, though it will still need to ensure the metadata is updated instead of (or as well as?) writing the `backup.info` file).

#### RetentionPolicy

RetentionPolicy currently uses the catalog built from `backup.info` files and this will need to be migrated to use the Storage Metadata.

#### Server

##### cron

The cron functions will need to look something like this (~ indicates existing functionality which will need to change, + indicates new functionality):

* sync
* ~archive WALs
* ~receive WALs
* ~check backups
* +backup placements
* ~retention:
  * +local retention
  * +offsite retention

## Roadmap

How do we actually get from current barman to the tiered storage?
We need to do this in discrete steps each of which take us closer to the goal without changing anything user facing.

1. Replace `xlog.db` with Storage Metadata.
2. Use Storage Metadata to store `backup.info` metadata (but continue to save everything to file too).
3. Add the Storage Manager and Raw tier and update barman to use it.
4. Add the Processed tier with one processing operation available (compression).
5. Add the off-site tiers.

### Replace `xlog.db` with Storage Metadata

The idea here is to get rid of `xlog.db` and instead handle the same data using a Storage Metadata manager/service/thing.
As well as storing everything needed by `xlog.db` it should also be "tier aware", even though WALs will currently only be available in one tier (the raw tier).
As with the original `xlog.db` it should be possible to rebuild the WAL metadata from the filesystem.

#### User facing changes

None.

#### New barman components

* Storage Metadata.

#### Changes to existing components

* Everything in barman which deals with `xlog.db`.

### Store `backup.info` in Storage Metadata

The Storage Metadata is extended to include the information stored in `backup.info` files.
Barman is updated to use Storage Metadata instead of `backup.info` files, though they are still written when backups are taken.
It should be possible to rebuild the Storage Metadata from the `backup.info` files.

#### User facing changes

None.
The risk profile has changed a bit given there is now a central place where backup metadata is stored (and therefore can be corrupted or lost).

#### New barman components

None.

#### Changes to existing components

* Storage Metadata now stores `backup.info` data.
* Annotations are also stored in Storage Metadata.
* RetentionPolicy interacts with Storage Metadata instead of `backup.info` files.
* BackupExecutor writes `backup.info` data to Storage Metadata.
* Show/list backup gets backup information from Storage Metadata.
* Other parts of barman which interact with `backup.info` files use Storage Metadata.

### Add the Storage Manager and Raw tier and update barman to use it

The Storage Manager is added along with a single tier which really just represents the current backups written to the filesystem by barman.
Barman is updated to use the Storage Manager for restoring backups.

#### User facing changes

* show/list backup display the location of backups (which will always be Raw)

#### New barman components

* Storage Manager.
* Raw Tier.

#### Changes to existing components

* Storage Manager returns backup location to show/list commands.
* Restore code uses the Storage Manager to find the backup and initiate restore instead of its knowledge about the barman storage layout.

### Add the Processed tier with one processing operation available (compression)

This is the first change which is visible to the user.
We add the Processed tier and update Barman cron so that it moves backups to the Processed tier if `minimum_raw_backups` is configured.

#### User facing changes

The following configuration variables will be available to the user:

* `minimum_raw_backups`
  * Leave unset for the existing behavior - retention policy applies to raw tier only and backups stay in their raw state.
  * Set to N and, once the number of raw backups exceeds N, `barman cron` will process them and they will be moved to the processed tier.
  * The retention policy will now apply to the processed tier instead of the raw tier.

#### New barman components

* Processed tier.

#### Changes to existing components

* Storage Manager needs to implement backup placement - so that moves from Raw to Processed can be triggered.
* Storage Manager also needs to copy WALs to the Processed tier.
* The RetentionPolicy needs to use the Processed tier, not the Raw tier, but only if `minimum_raw_backups` is configured.
* `barman cron` must now trigger the backup placement in the Storage Manager.
* `barman cron` must also cleanup up WALs in the Raw tier.
* The recovery code must use Storage Manager to stage a Processed backup for recovery and then recover from it as usual.

### Add the Offsite tiers

Here we add a second location where backups can reside.

#### User facing changes

The following configuration variables will be available to the user:

* `offsite_transfer`
  * The interval at which the most recent Processed backup should be transferred to an off-site location.
* `offsite_retention_policy`
  * The retention policy to be applied to the off-site tier.

#### New barman components

* Offsite tier.
* Cloud-specific offsite tiers for supported cloud providers.

#### Changes to existing components

* Storage Manager needs to determine Processed to Offsite moves when calculating backup placement.
* Storage Manager also needs to copy WALs to the Offsite tier.
* The RetentionPolicy needs to be run for the Offsite tier in addition to the Processed tier.
* `baran cron` must also cleanup WALs in the Offsite tier.
* The recovery code must use Storage Manager to stage an Offsite backup for recovery and then recover from it as usual.