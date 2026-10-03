import asyncio
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
