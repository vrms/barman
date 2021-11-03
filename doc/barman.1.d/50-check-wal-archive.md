check-wal-archive *SERVER_NAME*
:   Check that the WAL archive destination for *SERVER_NAME*
    is safe to use for a new PostgreSQL cluster. With no
    optional args (the default) this will pass if the WAL
    archive is empty and fail otherwise.

    --current-wal-segment [WAL_SEGMENT_ID]
    :   the full WAL segment ID, including logical ID, of the latest known
        WAL. If used with `--current-timeline` then the behaviour of the check
        is changed such that it passes if all WAL content in the archive is
        earlier than the specified WAL segment and timeline. If there are any
        files which are equal to or later than the current segment and timeline
        then the check will fail.

    --current-timeline [TIMELINE]
    :   a positive integer specifying the latest timeline. If used with
        `--current-wal-segment` then the behaviour of the check is changed such
        that it passes if all WAL content in the archive is earlier than the
        specified WAL segment and timeline. If there are any files which are
        equal to or later than the current segment and timeline then the check
        will fail.
