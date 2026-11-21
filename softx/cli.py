"""Export a completed local database or build a reproducible test snapshot."""
import argparse
import json
from pathlib import Path

from .app import DATA, ingest, worker
from .derived import publish
from .export import directory_export
from .science import recheck
from .store import db, rows


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination",type=Path)
    parser.add_argument("--fixtures",action="store_true",help="Populate a fresh SOFTX_DATA with labeled synthetic PDFs")
    args=parser.parse_args()
    if args.fixtures:
        with db() as connection:
            if connection.execute("SELECT COUNT(*) FROM pdf_version").fetchone()[0]:
                parser.error(f"--fixtures requires a fresh SOFTX_DATA directory ({DATA})")
        folder=Path(__file__).resolve().parents[1]/"fixtures"
        for path in sorted(folder.rglob("*.pdf")):
            ingest(path.read_bytes(),path.name)
        worker.shutdown()
        with db() as connection:
            for plot in rows(connection,"SELECT * FROM plot_dataset WHERE plot_type IN ('stability','EQE')"):
                metric="T80" if plot["plot_type"]=="stability" else "Jsc_EQE"
                publish(connection,plot,metric)
                recheck(connection,plot["publication_id"])
    counts=directory_export(args.destination)
    print(json.dumps(counts,ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
