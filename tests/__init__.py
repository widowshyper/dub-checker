import logging

# Keep expected warnings (a deliberately broken file, a failed download) out of the test output.
logging.getLogger("dubchecker").setLevel(logging.ERROR)
