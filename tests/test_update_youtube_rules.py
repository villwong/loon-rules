from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts import update_youtube_rules as updater


class YouTubeRuleUpdaterTests(unittest.TestCase):
    def test_v2fly_conversion_and_attribute_removal(self) -> None:
        source = """
        # comment
        example.com @cn
        domain:example.org @!cn
        full:www.example.net @ads
        keyword:video @ads
        regexp:^unsupported$
        """

        self.assertEqual(
            updater.parse_v2fly(source),
            [
                "DOMAIN-SUFFIX,example.com",
                "DOMAIN-SUFFIX,example.org",
                "DOMAIN,www.example.net",
                "DOMAIN-KEYWORD,video",
            ],
        )

    def test_blackmatrix_supported_types_and_extra_fields_are_preserved(self) -> None:
        source = """
        # metadata
        DOMAIN,www.example.com
        DOMAIN-SUFFIX,example.com
        DOMAIN-KEYWORD,example
        USER-AGENT,*Example App*
        IP-CIDR,192.0.2.0/24,no-resolve
        IP-CIDR6,2001:db8::/32,no-resolve
        URL-REGEX,^https://example.com
        """

        self.assertEqual(
            updater.parse_blackmatrix(source),
            [
                "DOMAIN,www.example.com",
                "DOMAIN-SUFFIX,example.com",
                "DOMAIN-KEYWORD,example",
                "USER-AGENT,*Example App*",
                "IP-CIDR,192.0.2.0/24,no-resolve",
                "IP-CIDR6,2001:db8::/32,no-resolve",
            ],
        )

    def test_deduplication_keeps_domain_types_distinct(self) -> None:
        blackmatrix = [
            "DOMAIN,example.com",
            "DOMAIN-SUFFIX,example.com",
            "DOMAIN-SUFFIX,shared.example",
        ]
        v2fly = [
            "DOMAIN,EXAMPLE.COM",
            "DOMAIN-SUFFIX,example.com",
            "DOMAIN-SUFFIX,shared.example",
        ]

        self.assertEqual(updater.merge_rules(v2fly, blackmatrix), blackmatrix)

    def test_current_output_keeps_timestamp_when_rules_do_not_change(self) -> None:
        rules = ["DOMAIN-SUFFIX,example.com"]
        old_time = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        existing = updater.build_output(rules, old_time)

        self.assertTrue(updater.is_current_output(existing, rules))
        self.assertFalse(
            updater.is_current_output(existing, ["DOMAIN-SUFFIX,changed.example"])
        )

    def test_failed_second_download_does_not_touch_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "rule" / "YouTube.list"
            output.parent.mkdir()
            output.write_text("existing rules\n", encoding="utf-8")

            def failing_fetcher(url: str) -> str:
                if url == updater.V2FLY_URL:
                    return "\n".join(f"video{i}.example" for i in range(100))
                raise updater.UpdateError("simulated Blackmatrix7 download failure")

            with self.assertRaises(updater.UpdateError):
                updater.update_rules(output, fetcher=failing_fetcher)

            self.assertEqual(output.read_text(encoding="utf-8"), "existing rules\n")


if __name__ == "__main__":
    unittest.main()
