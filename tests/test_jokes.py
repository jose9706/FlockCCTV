import asyncio
import re
import unittest

from flock_cctv.jokes import SharedRoastCooldown, make_roast


class MakeRoastTests(unittest.TestCase):
    def test_no_statistics_means_no_joke(self):
        self.assertIsNone(make_roast({"messages": 0, "voice_seconds": 0, "active_days": 0}, "this week"))

    def test_joke_uses_a_recorded_value(self):
        joke = make_roast(
            {"messages": 1234, "voice_seconds": 0, "active_days": 0},
            "this week",
            chooser=lambda options: options[0],
        )
        self.assertIn("1,234 messages", joke)
        self.assertIn("this week", joke)
        self.assertNotIn("@", joke)

    def test_voice_joke_labels_duration_as_observed(self):
        joke = make_roast(
            {"messages": 0, "voice_seconds": 3661, "active_days": 0},
            "this month",
            chooser=lambda options: options[0],
        )
        self.assertEqual(joke, "1h 1m in observed voice this month; the headset is earning its keep.")


    def test_joke_can_address_the_person_by_escaped_name(self):
        joke = make_roast(
            {"messages": 5, "voice_seconds": 0, "active_days": 0},
            "this week",
            name="Ana",
            chooser=lambda options: options[0],
        )
        self.assertEqual(
            joke, "Ana, 5 messages this week? Your keyboard deserves a lunch break."
        )

    def test_name_cannot_format_ping_or_break_lines(self):
        stats = {"messages": 5, "voice_seconds": 0, "active_days": 0}
        for name in ("**bold** _x_", "@everyone", "<@123456789012345678>", "<@&123456789012345678> @here", "Line\n@everyone\nmore"):
            with self.subTest(name=name):
                joke = make_roast(stats, "today", name=name, chooser=lambda options: options[0])
                self.assertEqual(len(joke.splitlines()), 1)
                self.assertNotIn("**bold**", joke)
                self.assertNotIn("@everyone", joke)
                self.assertNotIn("@here", joke)
                self.assertIsNone(re.search(r"<@[!&]?\d+>", joke))
        escaped = make_roast(stats, "today", name="**bold** _x_", chooser=lambda options: options[0])
        self.assertTrue(escaped.startswith("\\*\\*bold\\*\\* \\_x\\_, 5 messages today?"))

    def test_long_names_are_shortened_and_blank_names_ignored(self):
        stats = {"messages": 5, "voice_seconds": 0, "active_days": 0}
        plain = make_roast(stats, "today", chooser=lambda options: options[0])
        for blank in (None, "", "  \n "):
            self.assertEqual(
                make_roast(stats, "today", name=blank, chooser=lambda options: options[0]), plain
            )
        long_name = make_roast(stats, "today", name="x" * 200, chooser=lambda options: options[0])
        self.assertEqual(long_name, f"{'x' * 48}, {plain}")

    def test_no_statistics_means_no_joke_even_with_a_name(self):
        self.assertIsNone(
            make_roast({"messages": 0, "voice_seconds": 0, "active_days": 0}, "today", name="Ana")
        )


class SharedRoastCooldownTests(unittest.IsolatedAsyncioTestCase):
    async def test_cooldown_is_shared_and_expires(self):
        cooldown = SharedRoastCooldown(seconds=30)
        self.assertEqual(await cooldown.consume(now=100), 0)
        self.assertEqual(await cooldown.consume(now=110), 20)
        self.assertEqual(await cooldown.consume(now=130), 0)

    async def test_concurrent_requests_only_consume_one_slot(self):
        cooldown = SharedRoastCooldown(seconds=30)
        results = await asyncio.gather(*(cooldown.consume(now=100) for _ in range(8)))
        self.assertEqual(results.count(0), 1)
        self.assertEqual(results.count(30), 7)


if __name__ == "__main__":
    unittest.main()
