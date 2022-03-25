import logging
import typing
from concurrent import futures
from pathlib import Path

import click
import httpx
from lxml import etree

from .. import utils
from ..commands import _ERROR_COLOR
from .csw import csw_downloader

logger = logging.getLogger(__name__)

_xml_parser = etree.XMLParser(resolve_entities=False)

_DEFAULT_LEGACY_SASDI_RECORD_DIR = (
    Path.home() / "data/storage/legacy_sasdi_downloader/csw_records"
)
_DEFAULT_LEGACY_SASDI_THUMBNAIL_DIR = (
    Path.home() / "data/storage/legacy_sasdi_downloader/thumbnails"
)
_DEFAULTS_SAEON_ODP_RECORDS_DIR = (
    Path.home() / "data/storage/legacy_sasdi_downloader/saeon_odp_records"
)

_DEFAULT_MAX_WORKERS = 5


@click.group()
def legacy_sasdi():
    """Commands that rely on the CSW interface to the legacy SASDI"""


@legacy_sasdi.group()
def csw():
    """Commands that rely on the CSW interface to the legacy SASDI"""


@csw.command()
@click.option(
    "--url",
    default="http://app01.saeon.ac.za/PLATFORM_TEST/MAP/csw.asp",
    show_default=True,
    help="Legacy SASDI CSW endpoint",
)
@click.option("--page-size", type=int, default=20, show_default=True)
@click.option(
    "--output-dir",
    type=click.types.Path(),
    default=_DEFAULT_LEGACY_SASDI_RECORD_DIR,
    show_default=True,
)
@click.option(
    "--max-workers", type=int, default=_DEFAULT_MAX_WORKERS, show_default=True
)
def download_records(url: str, page_size: int, output_dir: Path, max_workers):
    """download catalogue records from the legacy SASDI

    Uses the legacy SASDI CSW interface to retrieve existing catalogue records with
    the csw:Record typename.

    """

    with httpx.Client() as client:
        try:
            total_records = csw_downloader.find_total_records(
                url, client=client, xml_parser=_xml_parser
            )
            logger.debug(f"{total_records=}")
        except httpx.ConnectError:
            logger.exception(msg=f"Could not connect to {url!r}")
        except httpx.ReadTimeout:
            logger.exception(msg=f"Connection timed out {url!r}")
        else:
            total = total_records or 0
            if total > 0:
                num_pages, remainder = divmod(total, page_size)
                if remainder > 0:
                    num_pages += 1
                logger.debug(f"{num_pages=}")
                execution_kwargs = []
                for page in range(num_pages):
                    kwargs = {
                        "limit": page_size,
                        "offset": (page * page_size),
                        "client": client,
                        "xml_parser": _xml_parser,
                    }
                    execution_kwargs.append(kwargs)
                workers = min(max_workers, num_pages)
                errors = csw_downloader.download_records_threaded_execution(
                    url, execution_kwargs, workers, output_dir
                )
                if len(errors) > 0:
                    logger.warning(f"{len(errors)} pages failed, retrying...")
                    final_errors = csw_downloader.retry_download_errors(
                        url, errors, max_workers, output_dir
                    )
                    if len(final_errors) > 0:
                        logger.warning(
                            f"Got {len(final_errors)} final errors, after retrying. "
                            f"Could not fetch all pages"
                        )
            else:
                logger.warning(
                    f"Could not find any records on CSW catalogue at {url!r}"
                )


@csw.command()
@click.option(
    "--records-dir",
    type=click.types.Path(),
    default=_DEFAULT_LEGACY_SASDI_RECORD_DIR,
    show_default=True,
)
@click.option(
    "--output-dir",
    type=click.types.Path(),
    default=_DEFAULT_LEGACY_SASDI_THUMBNAIL_DIR,
    show_default=True,
)
@click.option(
    "--max-workers", type=int, default=_DEFAULT_MAX_WORKERS, show_default=True
)
def retrieve_thumbnails(records_dir: Path, output_dir: Path, max_workers: int):
    """Retrieve thumbnails for previously downloaded legacy SASDI records"""
    num_retrieved = 0
    page_size = 10
    with httpx.Client() as client:
        batch = []
        for idx, path in enumerate(records_dir.iterdir()):
            logger.debug(f"({idx + 1}) Processing path {path!r}...")
            if path.is_file():
                record = csw_downloader.parse_record(
                    path,
                    csw_downloader.CSW_NAMESPACES,
                    xml_parser=_xml_parser,
                )
                batch.append(record)
            if len(batch) == page_size:
                downloaded_paths = _concurrent_thumbnail_download(
                    batch, output_dir, client=client, num_workers=max_workers
                )
                num_retrieved += len(downloaded_paths)
                batch = []
        else:  # download last ones
            downloaded_paths = _concurrent_thumbnail_download(
                batch, output_dir, client=client, num_workers=max_workers
            )
            num_retrieved += len(downloaded_paths)
    logger.info(f"Retrieved {num_retrieved} thumbnails")


@csw.command()
@click.option(
    "--records-dir",
    type=click.types.Path(),
    default=_DEFAULT_LEGACY_SASDI_RECORD_DIR,
    show_default=True,
)
@click.option(
    "--thumbnails-dir",
    type=click.types.Path(),
    default=_DEFAULT_LEGACY_SASDI_THUMBNAIL_DIR,
    show_default=True,
)
def import_records(records_dir: Path, thumbnails_dir: Path):
    """Import previously downloaded legacy SASDI records into the EMC"""
    # need to:
    # - ensure organizations are created, according to the import mappings
    # - ensure each org has at least an admin user, which will be used to perform the import
    # - convert records to data_dicts (taking thumbnail into account)
    # - create those records which do not exist yet
    seen_orgs: typing.Dict[str, typing.Dict] = {}
    for item in (i for i in records_dir.iterdir() if i.is_file()):
        record = csw_downloader.parse_record(
            item, csw_downloader.CSW_NAMESPACES, xml_parser=_xml_parser
        )
        target_org = record.mapped_owner_org()
        if target_org is None:
            pass  # this will be imported into the unsorted org
        elif target_org not in seen_orgs.keys():
            organization, _ = utils.maybe_create_organization(target_org)
            seen_orgs[organization["name"]] = organization
        else:
            organization = seen_orgs[target_org]
        owner_user = None
    click.secho("Not implemented yet", fg=_ERROR_COLOR)


def _concurrent_thumbnail_download(
    records: typing.List[csw_downloader.CswRecord],
    output_dir: Path,
    *,
    client: httpx.Client,
    num_workers: int,
) -> typing.List[Path]:
    """Download and save thumbnails using concurrent techniques"""
    result = []
    with futures.ThreadPoolExecutor(num_workers) as executor:
        to_do = {}
        for record in records:
            future = executor.submit(
                csw_downloader.retrieve_thumbnail,
                record,
                output_dir,
                client=client,
            )
            to_do[future] = record
        for future in futures.as_completed(to_do.keys()):
            record = to_do[future]
            try:
                thumbnail_path = future.result()
            except httpx.ReadTimeout:
                logger.exception(
                    f"Request timed out for {record.identifier!r}, skipping..."
                )
            else:
                if thumbnail_path is not None:
                    logger.info(f"Gotten {thumbnail_path!r}")
                    result.append(thumbnail_path)
    return result
