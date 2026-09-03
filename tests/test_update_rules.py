from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts import update_rules as updater


def service_config(name: str, output: str, url: str, format_name: str) -> dict:
    source: dict[str, object] = {
        "name": f"{name} source",
        "url": url,
        "format": format_name,
        "min_rules": 1,
    }
    if format_name == "loon-list":
        source["include_types"] = sorted(updater.SUPPORTED_LOON_TYPES)
    return {
        "name": name,
        "output": output,
        "header": f"自动生成 {name}",
        "sources": [source],
    }


class RuleUpdaterTests(unittest.TestCase):
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

    def test_loon_supported_types_and_extra_fields_are_preserved(self) -> None:
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
            updater.parse_loon(source, updater.SUPPORTED_LOON_TYPES),
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
        rules = [
            "DOMAIN,example.com",
            "DOMAIN-SUFFIX,example.com",
            "DOMAIN,EXAMPLE.COM",
            "DOMAIN-SUFFIX,example.com",
        ]

        self.assertEqual(
            updater.deduplicate(rules),
            ["DOMAIN,example.com", "DOMAIN-SUFFIX,example.com"],
        )

    def test_configuration_drives_multiple_services_and_no_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "rules.json"
            config_path.write_text(
                json.dumps(
                    {
                        "services": [
                            service_config(
                                "One",
                                "rule/One.list",
                                "https://example.test/one",
                                "v2fly-domain-list",
                            ),
                            service_config(
                                "Two",
                                "rule/Two.list",
                                "https://example.test/two",
                                "loon-list",
                            ),
                        ]
                    }
                ),
                encoding="utf-8",
            )
            responses = {
                "https://example.test/one": "one.example @cn\nfull:exact.one.example",
                "https://example.test/two": (
                    "USER-AGENT,*Two*\nIP-CIDR,192.0.2.0/24,no-resolve"
                ),
            }
            first_time = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
            second_time = datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc)

            changed = updater.update_all(
                config_path,
                root,
                fetcher=responses.__getitem__,
                updated_at=first_time,
            )
            one_path = root / "rule" / "One.list"
            two_path = root / "rule" / "Two.list"
            first_contents = (
                one_path.read_text(encoding="utf-8"),
                two_path.read_text(encoding="utf-8"),
            )

            self.assertEqual(changed, (one_path, two_path))
            self.assertIn("DOMAIN-SUFFIX,one.example", first_contents[0])
            self.assertIn("DOMAIN,exact.one.example", first_contents[0])
            self.assertIn("USER-AGENT,*Two*", first_contents[1])
            self.assertIn("IP-CIDR,192.0.2.0/24,no-resolve", first_contents[1])

            self.assertEqual(
                updater.update_all(
                    config_path,
                    root,
                    fetcher=responses.__getitem__,
                    updated_at=second_time,
                ),
                (),
            )
            self.assertEqual(
                (
                    one_path.read_text(encoding="utf-8"),
                    two_path.read_text(encoding="utf-8"),
                ),
                first_contents,
            )

    def test_later_download_failure_does_not_touch_any_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "rules.json"
            config_path.write_text(
                json.dumps(
                    {
                        "services": [
                            service_config(
                                "One",
                                "rule/One.list",
                                "https://example.test/one",
                                "v2fly-domain-list",
                            ),
                            service_config(
                                "Two",
                                "rule/Two.list",
                                "https://example.test/two",
                                "v2fly-domain-list",
                            ),
                        ]
                    }
                ),
                encoding="utf-8",
            )
            rule_dir = root / "rule"
            rule_dir.mkdir()
            one_path = rule_dir / "One.list"
            two_path = rule_dir / "Two.list"
            one_path.write_text("existing one\n", encoding="utf-8")
            two_path.write_text("existing two\n", encoding="utf-8")

            def failing_fetcher(url: str) -> str:
                if url.endswith("/one"):
                    return "one.example"
                raise updater.UpdateError("simulated later download failure")

            with self.assertRaises(updater.UpdateError):
                updater.update_all(config_path, root, fetcher=failing_fetcher)

            self.assertEqual(one_path.read_text(encoding="utf-8"), "existing one\n")
            self.assertEqual(two_path.read_text(encoding="utf-8"), "existing two\n")

    def test_truncated_source_does_not_replace_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = service_config(
                "Protected",
                "rule/Protected.list",
                "https://example.test/protected",
                "v2fly-domain-list",
            )
            service["sources"][0]["min_rules"] = 2
            config_path = root / "rules.json"
            config_path.write_text(
                json.dumps({"services": [service]}),
                encoding="utf-8",
            )
            output = root / "rule" / "Protected.list"
            output.parent.mkdir()
            output.write_text("existing rules\n", encoding="utf-8")

            with self.assertRaisesRegex(updater.UpdateError, "yielded only 1 rule"):
                updater.update_all(
                    config_path,
                    root,
                    fetcher=lambda _url: "only-one.example",
                )

            self.assertEqual(output.read_text(encoding="utf-8"), "existing rules\n")

    def test_repository_config_defines_youtube(self) -> None:
        services = updater.load_config(updater.DEFAULT_CONFIG_PATH)

        self.assertEqual(len(services), 1)
        self.assertEqual(services[0].name, "YouTube")
        self.assertEqual(services[0].output, Path("rule/YouTube.list"))
        self.assertEqual(
            [source.format_name for source in services[0].sources],
            ["loon-list", "v2fly-domain-list"],
        )

    def test_rejects_output_outside_rule_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "rules.json"
            config_path.write_text(
                json.dumps(
                    {
                        "services": [
                            service_config(
                                "Unsafe",
                                "../Unsafe.list",
                                "https://example.test/unsafe",
                                "loon-list",
                            )
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(updater.UpdateError, "safe relative path"):
                updater.load_config(config_path)


if __name__ == "__main__":
    unittest.main()
