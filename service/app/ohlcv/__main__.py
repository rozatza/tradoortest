"""``python -m app.ohlcv`` runs the backfill CLI."""

from app.ohlcv.backfill import main

if __name__ == "__main__":
    raise SystemExit(main())
