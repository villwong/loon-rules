from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts import update_rules as updater


def source_config(name: str, url: str, format_name: str) -> dict:
    source: dict[str, object] = {
        "name": name,
        "url": url,
        "format": format_name,
        "min_rules": 1,
    }
    if format_name == "loon-list":
        source["include_types"] = sorted(updater.SUPPORTED_LOON_TYPES)
    return source


def service_config(name: str, output: str, url: str, format_name: str) -> dict:
    return {
        "name": name,
        "output": output,
        "header": f"自动生成 {name}",
        "sources": [source_config(f"{name} source", url, format_name)],
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

    def test_unsupported_v2fly_regexp_is_not_case_mutated(self) -> None:
        entry = updater.parse_v2fly_entry(
            r"regexp:^chatgpt-[A-Z]+-\S+\.example$"
        )

        self.assertIsInstance(entry, updater.V2flyEntry)
        self.assertEqual(entry.value, r"^chatgpt-[A-Z]+-\S+\.example$")

    def test_v2fly_recursively_expands_nested_includes_then_deduplicates(self) -> None:
        root_url = "https://example.test/data/root"
        responses = {
            root_url: "root.example\ninclude:child\nfull:after.example @cn",
            "https://example.test/data/child": (
                "domain:child.example @!cn\ninclude:grandchild @ads\nroot.example"
            ),
            "https://example.test/data/grandchild": "keyword:nested @ads",
        }

        rules = updater.expand_v2fly_source(root_url, responses.__getitem__)

        self.assertEqual(
            rules,
            [
                "DOMAIN-SUFFIX,root.example",
                "DOMAIN-SUFFIX,child.example",
                "DOMAIN-KEYWORD,nested",
                "DOMAIN,after.example",
            ],
        )
        self.assertFalse(any("@" in rule for rule in rules))

        expanded = updater.expand_v2fly_entries(root_url, responses.__getitem__)
        sources_by_value = {entry.value: entry.source_categories for entry in expanded}
        self.assertEqual(sources_by_value["root.example"], ("root", "child"))
        self.assertEqual(sources_by_value["child.example"], ("child",))
        self.assertEqual(sources_by_value["nested"], ("grandchild",))

    def test_v2fly_include_attribute_filters(self) -> None:
        root_url = "https://example.test/data/root"
        responses = {
            root_url: "include:child @ads @-cn",
            "https://example.test/data/child": (
                "ads.example @ads\n"
                "ads-cn.example @ads @cn\n"
                "plain.example\n"
                "china.example @cn"
            ),
        }

        self.assertEqual(
            updater.expand_v2fly_source(root_url, responses.__getitem__),
            ["DOMAIN-SUFFIX,ads.example"],
        )

    def test_v2fly_reuses_include_downloads(self) -> None:
        root_url = "https://example.test/data/root"
        shared_url = "https://example.test/data/shared"
        responses = {
            root_url: "include:shared\ninclude:shared",
            shared_url: "shared.example",
        }
        fetch_count: dict[str, int] = {}

        def fetcher(url: str) -> str:
            fetch_count[url] = fetch_count.get(url, 0) + 1
            return responses[url]

        self.assertEqual(
            updater.expand_v2fly_source(root_url, fetcher),
            ["DOMAIN-SUFFIX,shared.example"],
        )
        self.assertEqual(fetch_count, {root_url: 1, shared_url: 1})

    def test_v2fly_rejects_circular_includes(self) -> None:
        root_url = "https://example.test/data/root"
        responses = {
            root_url: "include:child",
            "https://example.test/data/child": "include:root",
        }

        with self.assertRaisesRegex(updater.UpdateError, "circular v2fly include"):
            updater.expand_v2fly_source(root_url, responses.__getitem__)

    def test_v2fly_exclude_includes_is_recursive_and_cache_is_policy_specific(
        self,
    ) -> None:
        root_url = "https://example.test/data/google"
        responses = {
            root_url: "include:youtube\ninclude:google-core",
            "https://example.test/data/youtube": (
                "youtube-only.example\ncommon-infra.example"
            ),
            "https://example.test/data/google-core": (
                "common-infra.example\ngoogle-only.example\ninclude:youtube"
            ),
        }
        downloads: dict[str, str] = {}
        expansions: dict[
            tuple[str, frozenset[str]], tuple[updater.V2flyEntry, ...]
        ] = {}

        complete = updater.expand_v2fly_source(
            root_url,
            responses.__getitem__,
            downloads,
            expansions,
        )
        isolated = updater.expand_v2fly_source(
            root_url,
            responses.__getitem__,
            downloads,
            expansions,
            ("youtube",),
        )

        self.assertIn("DOMAIN-SUFFIX,youtube-only.example", complete)
        self.assertNotIn("DOMAIN-SUFFIX,youtube-only.example", isolated)
        self.assertEqual(
            isolated,
            [
                "DOMAIN-SUFFIX,common-infra.example",
                "DOMAIN-SUFFIX,google-only.example",
            ],
        )

    def test_service_boundaries_are_stable_without_final_set_subtraction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "rules.json"
            config_path.write_text(
                json.dumps(
                    {
                        "services": [
                            service_config(
                                "YouTube",
                                "rule/YouTube.list",
                                "https://example.test/data/youtube",
                                "v2fly-domain-list",
                            ),
                            {
                                "name": "Google",
                                "output": "rule/Google.list",
                                "header": "自动生成 Google",
                                "exclude_includes": ["youtube"],
                                "sources": [
                                    {
                                        "name": "Blackmatrix7 Google",
                                        "url": "https://example.test/black-google",
                                        "format": "loon-list",
                                        "min_rules": 1,
                                        "include_types": sorted(
                                            updater.SUPPORTED_LOON_TYPES
                                        ),
                                    },
                                    {
                                        "name": "v2fly Google",
                                        "url": "https://example.test/data/google",
                                        "format": "v2fly-domain-list",
                                        "min_rules": 1,
                                    },
                                ],
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            responses = {
                "https://example.test/data/youtube": (
                    "youtube-only.example\n"
                    "explicit-overlap.example\n"
                    "common-infra.example"
                ),
                "https://example.test/data/google": (
                    "include:youtube\ninclude:google-core"
                ),
                "https://example.test/data/google-core": (
                    "common-infra.example\ngoogle-only.example"
                ),
                "https://example.test/black-google": (
                    "DOMAIN-SUFFIX,black-google.example\n"
                    "DOMAIN-SUFFIX,explicit-overlap.example"
                ),
            }
            first_time = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
            second_time = datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc)

            updater.update_all(
                config_path,
                root,
                fetcher=responses.__getitem__,
                updated_at=first_time,
            )
            youtube_path = root / "rule" / "YouTube.list"
            google_path = root / "rule" / "Google.list"
            youtube_contents = youtube_path.read_text(encoding="utf-8")
            google_contents = google_path.read_text(encoding="utf-8")

            self.assertIn("DOMAIN-SUFFIX,youtube-only.example", youtube_contents)
            self.assertIn("DOMAIN-SUFFIX,explicit-overlap.example", youtube_contents)
            self.assertNotIn("DOMAIN-SUFFIX,youtube-only.example", google_contents)
            # Explicit Blackmatrix7 Google rules remain even if YouTube has them.
            self.assertIn("DOMAIN-SUFFIX,explicit-overlap.example", google_contents)
            # A rule independently owned by another Google include is not subtracted.
            self.assertIn("DOMAIN-SUFFIX,common-infra.example", google_contents)

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
                youtube_path.read_text(encoding="utf-8"), youtube_contents
            )
            self.assertEqual(google_path.read_text(encoding="utf-8"), google_contents)

    def test_google_gemini_boundary_uses_source_tree_not_final_subtraction(
        self,
    ) -> None:
        google_url = "https://example.test/data/google"
        gemini_url = "https://example.test/data/google-gemini"
        responses = {
            google_url: (
                "shared-google.example\n"
                "include:google-gemini\n"
                "include:google-deepmind"
            ),
            gemini_url: "include:google-deepmind",
            "https://example.test/data/google-deepmind": (
                "gemini-only.example\nshared-google.example"
            ),
        }
        excluded = ("google-gemini", "google-deepmind")

        google_rules = updater.expand_v2fly_source(
            google_url,
            responses.__getitem__,
            exclude_includes=excluded,
        )
        gemini_rules = updater.expand_v2fly_source(
            gemini_url,
            responses.__getitem__,
        )

        self.assertNotIn("DOMAIN-SUFFIX,gemini-only.example", google_rules)
        self.assertIn("DOMAIN-SUFFIX,gemini-only.example", gemini_rules)
        self.assertIn("DOMAIN-SUFFIX,shared-google.example", google_rules)
        self.assertIn("DOMAIN-SUFFIX,shared-google.example", gemini_rules)

    def test_ai_services_merge_only_explicit_sources_and_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "rules.json"
            services = [
                {
                    "name": "Gemini",
                    "output": "rule/Gemini.list",
                    "header": "自动生成 Gemini",
                    "sources": [
                        source_config(
                            "Blackmatrix7 Gemini",
                            "https://example.test/black-gemini",
                            "loon-list",
                        ),
                        source_config(
                            "Blackmatrix7 BardAI",
                            "https://example.test/black-bard",
                            "loon-list",
                        ),
                        source_config(
                            "v2fly",
                            "https://example.test/data/google-gemini",
                            "v2fly-domain-list",
                        ),
                    ],
                },
                {
                    "name": "Anthropic",
                    "output": "rule/Anthropic.list",
                    "header": "自动生成 Anthropic",
                    "sources": [
                        source_config(
                            "Blackmatrix7 Anthropic",
                            "https://example.test/black-anthropic",
                            "loon-list",
                        ),
                        source_config(
                            "Blackmatrix7 Claude",
                            "https://example.test/black-claude",
                            "loon-list",
                        ),
                        source_config(
                            "v2fly",
                            "https://example.test/data/anthropic",
                            "v2fly-domain-list",
                        ),
                    ],
                },
                {
                    "name": "OpenAI",
                    "output": "rule/OpenAI.list",
                    "header": "自动生成 OpenAI",
                    "sources": [
                        source_config(
                            "Blackmatrix7 OpenAI",
                            "https://example.test/black-openai",
                            "loon-list",
                        ),
                        source_config(
                            "v2fly",
                            "https://example.test/data/openai",
                            "v2fly-domain-list",
                        ),
                    ],
                },
            ]
            config_path.write_text(
                json.dumps({"services": services}), encoding="utf-8"
            )
            responses = {
                "https://example.test/black-gemini": (
                    "DOMAIN-SUFFIX,bard.google.com\n"
                    "DOMAIN-KEYWORD,generativelanguage"
                ),
                "https://example.test/black-bard": (
                    "DOMAIN,generativelanguage.googleapis.com"
                ),
                "https://example.test/data/google-gemini": (
                    "include:google-deepmind"
                ),
                "https://example.test/data/google-deepmind": (
                    "gemini.google.com\nnotebooklm.google.com\n"
                    "jules.google\nflow.google\nopal.google"
                ),
                "https://example.test/black-anthropic": (
                    "DOMAIN-SUFFIX,anthropic.com\nDOMAIN-SUFFIX,claude.ai"
                ),
                "https://example.test/black-claude": (
                    "DOMAIN,cdn.usefathom.com\nDOMAIN-SUFFIX,claude.com"
                ),
                "https://example.test/data/anthropic": (
                    "anthropic.com\nclaude.ai\nclaude.com\nclau.de\n"
                    "claudemcpclient.com\nclaudemcpcontent.com\n"
                    "claudeusercontent.com"
                ),
                "https://example.test/black-openai": (
                    "DOMAIN-SUFFIX,openai.com\n"
                    "DOMAIN-SUFFIX,chatgpt.com\n"
                    "IP-CIDR,192.0.2.1/32,no-resolve"
                ),
                "https://example.test/data/openai": (
                    "openai.com\nchatgpt.com\nchat.com\n"
                    "full:openaiassets.blob.core.windows.net\n"
                    "regexp:^chatgpt-dynamic-[0-9]+\\.example$"
                ),
            }
            first_time = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
            second_time = datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc)

            updater.update_all(
                config_path,
                root,
                fetcher=responses.__getitem__,
                updated_at=first_time,
            )
            outputs = {
                name: (root / "rule" / f"{name}.list")
                for name in ("Gemini", "Anthropic", "OpenAI")
            }
            contents = {
                name: path.read_text(encoding="utf-8")
                for name, path in outputs.items()
            }

            gemini_v2 = set(
                updater.expand_v2fly_source(
                    "https://example.test/data/google-gemini",
                    responses.__getitem__,
                )
            )
            anthropic_v2 = set(
                updater.expand_v2fly_source(
                    "https://example.test/data/anthropic",
                    responses.__getitem__,
                )
            )
            openai_v2 = set(
                updater.expand_v2fly_source(
                    "https://example.test/data/openai",
                    responses.__getitem__,
                )
            )
            output_rules = {
                name: {
                    line
                    for line in text.splitlines()
                    if line and not line.startswith("#")
                }
                for name, text in contents.items()
            }

            self.assertLessEqual(gemini_v2, output_rules["Gemini"])
            self.assertLessEqual(anthropic_v2, output_rules["Anthropic"])
            self.assertLessEqual(openai_v2, output_rules["OpenAI"])
            self.assertIn("DOMAIN,cdn.usefathom.com", output_rules["Anthropic"])
            self.assertIn("DOMAIN-SUFFIX,chat.com", output_rules["OpenAI"])
            self.assertIn(
                "IP-CIDR,192.0.2.1/32,no-resolve", output_rules["OpenAI"]
            )

            self.assertNotIn("DOMAIN-SUFFIX,openai.com", output_rules["Gemini"])
            self.assertNotIn("DOMAIN-SUFFIX,anthropic.com", output_rules["Gemini"])
            self.assertNotIn("DOMAIN-SUFFIX,openai.com", output_rules["Anthropic"])
            self.assertNotIn("DOMAIN-SUFFIX,gemini.google.com", output_rules["Anthropic"])
            self.assertNotIn("DOMAIN-SUFFIX,anthropic.com", output_rules["OpenAI"])
            self.assertNotIn("DOMAIN-SUFFIX,gemini.google.com", output_rules["OpenAI"])
            self.assertFalse((root / "rule" / "Claude.list").exists())
            self.assertFalse((root / "rule" / "ChatGPT.list").exists())

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
                {
                    name: path.read_text(encoding="utf-8")
                    for name, path in outputs.items()
                },
                contents,
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
                "https://example.test/one": "one.example @cn\nfull:exact.other.example",
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
            self.assertIn("DOMAIN,exact.other.example", first_contents[0])
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
        services_by_name = {service.name: service for service in services}
        youtube = services_by_name["YouTube"]

        self.assertEqual(youtube.output, Path("rule/YouTube.list"))
        self.assertEqual(
            [source.format_name for source in youtube.sources],
            ["loon-list", "v2fly-domain-list"],
        )
        self.assertEqual(youtube.exclude_includes, ())

    def test_repository_config_defines_google_without_resolve_list(self) -> None:
        services = updater.load_config(updater.DEFAULT_CONFIG_PATH)
        services_by_name = {service.name: service for service in services}
        google = services_by_name["Google"]

        self.assertEqual(google.output, Path("rule/Google.list"))
        self.assertEqual(
            [source.format_name for source in google.sources],
            ["loon-list", "v2fly-domain-list"],
        )
        self.assertEqual(
            set(google.sources[0].include_types),
            updater.SUPPORTED_LOON_TYPES,
        )
        self.assertTrue(google.sources[0].url.endswith("/rule/Loon/Google/Google.list"))
        self.assertTrue(google.sources[1].url.endswith("/data/google"))
        self.assertGreaterEqual(google.sources[1].min_rules, 700)
        self.assertEqual(
            google.exclude_includes,
            ("youtube", "google-gemini", "google-deepmind"),
        )
        self.assertNotIn("Google_Resolve.list", google.sources[0].url)

    def test_repository_config_defines_only_explicit_ai_services(self) -> None:
        services = updater.load_config(updater.DEFAULT_CONFIG_PATH)
        services_by_name = {service.name: service for service in services}

        self.assertTrue(
            {"YouTube", "Google", "Gemini", "Anthropic", "OpenAI"}.issubset(
                services_by_name
            )
        )
        self.assertEqual(
            services_by_name["Gemini"].output, Path("rule/Gemini.list")
        )
        self.assertEqual(
            services_by_name["Anthropic"].output, Path("rule/Anthropic.list")
        )
        self.assertEqual(
            services_by_name["OpenAI"].output, Path("rule/OpenAI.list")
        )

        source_urls = {
            name: [source.url for source in services_by_name[name].sources]
            for name in ("Gemini", "Anthropic", "OpenAI")
        }
        self.assertTrue(source_urls["Gemini"][-1].endswith("/data/google-gemini"))
        self.assertTrue(source_urls["Anthropic"][-1].endswith("/data/anthropic"))
        self.assertTrue(source_urls["OpenAI"][-1].endswith("/data/openai"))
        self.assertTrue(any("/Loon/Gemini/" in url for url in source_urls["Gemini"]))
        self.assertTrue(any("/Loon/BardAI/" in url for url in source_urls["Gemini"]))
        self.assertTrue(
            any("/Loon/Anthropic/" in url for url in source_urls["Anthropic"])
        )
        self.assertTrue(
            any("/Loon/Claude/" in url for url in source_urls["Anthropic"])
        )
        self.assertTrue(any("/Loon/OpenAI/" in url for url in source_urls["OpenAI"]))
        self.assertFalse(
            any(
                "category-ai" in url.casefold()
                for urls in source_urls.values()
                for url in urls
            )
        )
        self.assertNotIn("Claude", services_by_name)
        self.assertNotIn("ChatGPT", services_by_name)

    def test_repository_config_defines_only_dedicated_requested_sources(self) -> None:
        services = updater.load_config(updater.DEFAULT_CONFIG_PATH)
        services_by_name = {service.name: service for service in services}
        expected_sources = {
            "Bybit": ("bybit", None),
            "Wise": ("wise", None),
            "OKX": ("okx", "OKX"),
            "Binance": ("binance", "Binance"),
            "Reddit": ("reddit", "Reddit"),
            "PayPal": ("paypal", "PayPal"),
            "eBay": ("ebay", "eBay"),
            "Twitter": ("twitter", "Twitter"),
            "Instagram": ("instagram", "Instagram"),
            "GitHub": ("github", "GitHub"),
            "Telegram": ("telegram", "Telegram"),
            "Microsoft": ("microsoft", "Microsoft"),
        }

        self.assertEqual(
            set(services_by_name),
            {
                "YouTube",
                "Google",
                "Gemini",
                "Anthropic",
                "OpenAI",
                *expected_sources,
            },
        )
        for name, (v2fly_category, blackmatrix_name) in expected_sources.items():
            with self.subTest(service=name):
                service = services_by_name[name]
                self.assertEqual(service.output, Path(f"rule/{name}.list"))
                self.assertTrue(
                    service.sources[-1].url.endswith(f"/data/{v2fly_category}")
                )
                if blackmatrix_name is None:
                    self.assertEqual(len(service.sources), 1)
                else:
                    self.assertEqual(len(service.sources), 2)
                    self.assertIn(
                        f"/rule/Loon/{blackmatrix_name}/{blackmatrix_name}.list",
                        service.sources[0].url,
                    )
                    self.assertNotIn("_Resolve.list", service.sources[0].url)

                source_urls = [source.url.casefold() for source in service.sources]
                self.assertFalse(
                    any(
                        marker in url
                        for marker in (
                            "/global/",
                            "/globalmedia/",
                            "/proxy/",
                            "/crypto/",
                            "/social/",
                            "category-",
                        )
                        for url in source_urls
                    )
                )

        self.assertNotIn("AlipayHK", services_by_name)
        self.assertFalse((updater.REPOSITORY_ROOT / "rule/AlipayHK.list").exists())
        self.assertFalse(
            any(
                marker in source.url.casefold()
                for service in services
                for source in service.sources
                for marker in ("/data/alipay", "/data/alibaba", "/data/ant-group")
            )
        )

    def test_requested_services_preserve_every_configured_source_rule(self) -> None:
        service_names = (
            "Bybit",
            "Wise",
            "OKX",
            "Binance",
            "Reddit",
            "PayPal",
            "eBay",
            "Twitter",
            "Instagram",
            "GitHub",
            "Telegram",
            "Microsoft",
        )
        v2fly_only = {"Bybit", "Wise"}
        responses: dict[str, str] = {}
        services: list[updater.ServiceConfig] = []
        expected_by_service: dict[str, set[str]] = {}

        for index, name in enumerate(service_names, start=1):
            category = name.casefold()
            v2fly_url = f"https://example.test/data/{category}"
            child_url = f"https://example.test/data/{category}-child"
            responses[v2fly_url] = (
                f"domain:{category}.example @cn\ninclude:{category}-child"
            )
            responses[child_url] = f"full:api.{category}-exact.example @ads"
            sources: list[updater.SourceConfig] = []
            expected = {
                f"DOMAIN-SUFFIX,{category}.example",
                f"DOMAIN,api.{category}-exact.example",
            }

            if name not in v2fly_only:
                blackmatrix_url = f"https://example.test/loon/{category}.list"
                responses[blackmatrix_url] = (
                    f"DOMAIN-SUFFIX,{category}.bm.example\n"
                    f"IP-CIDR,192.0.2.{index}/32,no-resolve"
                )
                sources.append(
                    updater.SourceConfig(
                        "Blackmatrix7",
                        blackmatrix_url,
                        "loon-list",
                        1,
                        tuple(sorted(updater.SUPPORTED_LOON_TYPES)),
                    )
                )
                expected.update(
                    {
                        f"DOMAIN-SUFFIX,{category}.bm.example",
                        f"IP-CIDR,192.0.2.{index}/32,no-resolve",
                    }
                )

            sources.append(
                updater.SourceConfig("v2fly", v2fly_url, "v2fly-domain-list", 1)
            )
            services.append(
                updater.ServiceConfig(
                    name,
                    Path(f"rule/{name}.list"),
                    f"自动生成 {name}",
                    tuple(sources),
                )
            )
            expected_by_service[name] = expected

        with tempfile.TemporaryDirectory() as directory:
            prepared = updater.prepare_services(
                services,
                Path(directory),
                responses.__getitem__,
            )

        self.assertEqual(len(prepared), len(service_names))
        for item in prepared:
            with self.subTest(service=item.service.name):
                self.assertEqual(
                    expected_by_service[item.service.name] - set(item.rules), set()
                )

    def test_generated_requested_rules_are_valid_and_contain_core_domains(self) -> None:
        required_rules = {
            "Bybit": {"DOMAIN-SUFFIX,bybit.com"},
            "Wise": {
                "DOMAIN-SUFFIX,transferwise.com",
                "DOMAIN-SUFFIX,wise.com",
            },
            "OKX": {"DOMAIN-SUFFIX,okex.com"},
            "Binance": {"DOMAIN-SUFFIX,binance.com"},
            "Reddit": {
                "DOMAIN-SUFFIX,reddit.com",
                "DOMAIN-SUFFIX,redd.it",
            },
            "PayPal": {"DOMAIN-SUFFIX,paypal.com"},
            "eBay": {"DOMAIN-SUFFIX,ebay.com"},
            "Twitter": {
                "DOMAIN-SUFFIX,twitter.com",
                "DOMAIN-SUFFIX,x.com",
            },
            "Instagram": {"DOMAIN-SUFFIX,instagram.com"},
            "GitHub": {
                "DOMAIN-SUFFIX,github.com",
                "DOMAIN-SUFFIX,githubusercontent.com",
            },
            "Telegram": {
                "DOMAIN-SUFFIX,telegram.org",
                "DOMAIN-SUFFIX,t.me",
                "DOMAIN-SUFFIX,telegra.ph",
            },
            "Microsoft": {"DOMAIN-SUFFIX,microsoft.com"},
        }

        for name, required in required_rules.items():
            with self.subTest(service=name):
                output = updater.REPOSITORY_ROOT / "rule" / f"{name}.list"
                self.assertTrue(output.is_file())
                rules = [
                    line.strip()
                    for line in output.read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.startswith("#")
                ]
                rule_set = set(rules)
                self.assertEqual(required - rule_set, set())
                self.assertEqual(len(rules), len({updater.rule_key(rule) for rule in rules}))
                self.assertTrue(
                    all(rule.split(",", 1)[0] in updater.SUPPORTED_LOON_TYPES for rule in rules)
                )
                self.assertFalse(
                    any(
                        token in rule
                        for rule in rules
                        for token in ("include:", "regexp:", " @", ":@")
                    )
                )
                self.assertTrue(
                    all(
                        rule.endswith(",no-resolve")
                        for rule in rules
                        if rule.startswith(("IP-CIDR,", "IP-CIDR6,"))
                    )
                )

        self.assertFalse((updater.REPOSITORY_ROOT / "rule/AlipayHK.list").exists())

    def test_google_merge_preserves_no_resolve_and_distinct_domain_types(self) -> None:
        services = updater.load_config(updater.DEFAULT_CONFIG_PATH)
        google = next(service for service in services if service.name == "Google")
        blackmatrix_rules = updater.parse_source(
            google.sources[0],
            """
            DOMAIN,exact.google.example
            DOMAIN-SUFFIX,exact.google.example
            DOMAIN-KEYWORD,google
            USER-AGENT,*GoogleApp*
            IP-CIDR,192.0.2.0/24,no-resolve
            IP-CIDR6,2001:db8::/32,no-resolve
            """,
        )
        v2fly_rules = updater.parse_source(
            google.sources[1],
            """
            domain:exact.google.example @cn
            full:exact.google.example @!cn
            keyword:google @ads
            new-google.example @ads
            """,
        )

        merged = updater.deduplicate([*blackmatrix_rules, *v2fly_rules])

        self.assertEqual(merged.count("DOMAIN,exact.google.example"), 1)
        self.assertEqual(merged.count("DOMAIN-SUFFIX,exact.google.example"), 1)
        self.assertEqual(merged.count("DOMAIN-KEYWORD,google"), 1)
        self.assertIn("DOMAIN-SUFFIX,new-google.example", merged)
        self.assertIn("IP-CIDR,192.0.2.0/24,no-resolve", merged)
        self.assertIn("IP-CIDR6,2001:db8::/32,no-resolve", merged)
        self.assertFalse(any("@cn" in rule or "@ads" in rule for rule in merged))

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
