from __future__ import annotations

import unittest
from unittest.mock import patch

import check_angelone_data
from config.settings import Settings


class AngelOneCheckTests(unittest.TestCase):
    def test_validate_settings_rejects_missing_credentials(self) -> None:
        settings = Settings(
            api_key="",
            client_id="client",
            pin="",
            totp_secret="",
            risk_per_trade_pct=1.0,
            capital=100000.0,
            scan_interval_seconds=2.0,
            alert_every_check=True,
            market_data_provider="angelone",
            order_product_type="INTRADAY",
            order_variety="NORMAL",
        )
        with patch("check_angelone_data.load_settings", return_value=settings):
            with self.assertRaises(ValueError) as ctx:
                check_angelone_data._validate_settings()
        self.assertIn("ANGELONE_API_KEY", str(ctx.exception))
        self.assertIn("ANGELONE_TOTP_SECRET", str(ctx.exception))

    def test_main_returns_success_when_run_check_passes(self) -> None:
        with patch("check_angelone_data.run_check", return_value=0) as run_check:
            exit_code = check_angelone_data.main(["--symbol", "SBIN.NS"])
        self.assertEqual(exit_code, 0)
        run_check.assert_called_once()


if __name__ == "__main__":
    unittest.main()
