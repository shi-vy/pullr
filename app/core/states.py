from enum import Enum, auto

class TorrentState(Enum):
    SENT_TO_REALDEBRID = auto()
    WAITING_FOR_REALDEBRID = auto()
    WAITING_FOR_SELECTION = auto()
    AVAILABLE_FROM_REALDEBRID = auto()
    DOWNLOADING_FROM_REALDEBRID = auto()
    TRANSFERRING_TO_MEDIA_SERVER = auto()
    FINISHED = auto()
    WAITING_FOR_MEDIA_SERVER = auto()
    FAILED = auto()

    def __str__(self):
        return self.name
