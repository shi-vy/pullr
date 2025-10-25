from enum import Enum, auto

class TorrentState(Enum):
    SENT_TO_REALDEBRID = auto()
    WAITING_FOR_REALDEBRID = auto()
    WAITING_FOR_SELECTION = auto()
    AVAILABLE_FROM_REALDEBRID = auto()
    WAITING_IN_QUEUE = auto()  # Ready to download but waiting for its turn
    DOWNLOADING_FROM_REALDEBRID = auto()
    TRANSFERRING_TO_MEDIA_SERVER = auto()
    FINISHED = auto()
    WAITING_FOR_MEDIA_SERVER = auto()
    FAILED = auto()

    def __str__(self):
        return self.name