import logging

# Most tests run without a performers file; its one "missing" error is expected there.
logging.getLogger("media_organizer.sorter").addHandler(logging.NullHandler())
