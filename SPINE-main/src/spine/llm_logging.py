import logging
import sys
from typing import Optional


def get_logger(
    name: str, level: int, stdout: Optional[bool] = True, fpath: Optional[str] = ""
) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level=level)

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    if stdout:
        sh = logging.StreamHandler(stream=sys.stdout)
        sh.setFormatter(formatter)
        logger.addHandler(sh)

    if fpath != "":
        fh = logging.FileHandler(filename=fpath)
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    return logger
