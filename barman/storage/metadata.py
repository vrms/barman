# Storage Metadata

import os

from abc import ABCMeta, abstractmethod
from contextlib import contextmanager
import shutil

from barman.infofile import WalFileInfo
from barman.utils import with_metaclass

# This is where we track the metadata for our backups and WALs.
# As well as the details of where backups and WALs are located we also
# move specific retrieval logic here.


def storage_metadata_factory(server, path):
    return StorageMetadataCsv(server, path)


class StorageMetadata(with_metaclass(ABCMeta)):
    def __init__(self, server):
        self.server = server

    @abstractmethod
    def get_wal_infos(self):
        """Return an interable of WalFileInfo objects"""

    @abstractmethod
    def write_wal_infos(self, wal_info):
        """Write the WalFileInfo to metadata storage"""


class StorageMetadataCsv(StorageMetadata):
    def __init__(self, server, path=""):
        super(StorageMetadataCsv, self).__init__(server)
        self.path = "%s/metadata.csv" % path
        self.current_position = 0

    def get_wal_info_at(self, last_position=0):
        try:
            wal_info = next(self.get_wal_infos(last_position))
            self.current_position = last_position
            return wal_info
        except StopIteration:
            return None

    def get_wal_infos(self, last_position=0):
        """
        last_position is the number of records not bytes so that we can apply the
        concept to other storage backends
        """
        # TODO Why do we allow calling code to set None?
        last_position = last_position or 0
        self.current_position = 0
        try:
            with open(self.path, "r") as db:
                while True:
                    line = db.readline().strip()
                    if len(line) == 0:
                        break
                    if self.current_position < last_position:
                        self.current_position += 1
                        continue
                    tokens = line.split("\t")

                    record_type = tokens[0]
                    if record_type == "WAL":
                        # TODO: this is just WalFileInfo.from_xlogdb_line
                        try:
                            _, name, size, time, compression = line.split()
                        except ValueError:
                            # Old format compatibility (no compression)
                            compression = None
                            try:
                                name, size, time = line.split()
                            except ValueError:
                                raise ValueError("cannot parse line: %r" % (line,))
                        # The to_xlogdb_line method writes None values as literal 'None'
                        if compression == "None":
                            compression = None
                        size = int(size)
                        time = float(time)
                        yield WalFileInfo(
                            name=name,
                            size=size,
                            time=time,
                            compression=compression,
                        )
                    self.current_position += 1
        except FileNotFoundError:
            return

    def _write_wal_info(self, wal_info):
        with open(self.path, "a+") as db:
            db.write(
                "WAL\t%s\t%s\t%s\t%s\n"
                % (
                    wal_info.name,
                    wal_info.size,
                    wal_info.time,
                    wal_info.compression,
                )
            )
            # flush and fsync for every line
            db.flush()
            os.fsync(db.fileno())

    def write_wal_infos(self, wal_infos):
        for wal_info in wal_infos:
            self._write_wal_info(wal_info)

    def has_content(self):
        try:
            with open(self.path, "r") as db:
                return len(db.readline().strip().split("\t")) > 0
        except FileNotFoundError:
            return False

    def delete_wal_info(self, wal_info):
        """This is pretty terrible but will do for now"""
        new_path = self.path + ".new"
        with open(self.path, "r+") as old:
            with open(new_path, "w+") as new:
                for line in old:
                    if wal_info.name not in line:
                        new.write(line)
                new.flush()
                new.seek(0)
                old.seek(0)
                shutil.copyfileobj(new, old)
                old.truncate()

    def truncate(self):
        with open(self.path("w")) as db:
            db.seek(0)
            db.truncate()


"""
import barman
from barman.storage.metadata import StorageMetadataCsv

meta = StorageMetadataCsv(barman.__config__, "mt-primary", "/tmp/metadata.csv")
wals = [wi for wi in meta.get_wal_infos()]
wals[0].name = wals[0].name + "ohai"
meta.write_wal_infos([wals[0]])
"""
