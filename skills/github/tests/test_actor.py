"""GraphQL actor resolution against literal-loopback REST identities."""
import copy
import tempfile
import unittest
from urllib.parse import unquote

from test_transport import Loopback
from gh_presence.http import HTTP
from gh_presence.provider import GitHub
from gh_presence.store import Store


class ActorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="github-actor-test-")
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.close)
        self.human = {"id": 903, "login": "fixture-app", "type": "User"}
        self.bot = {"id": 901, "login": "fixture-app[bot]", "type": "Bot"}
        self.responses = {"/user": (200, self.human, {}), "/users/fixture-app": (200, self.human, {}),
                          "/users/fixture-app[bot]": (200, self.bot, {})}
        self.server = Loopback(lambda method, path, data:
                               self.responses.get(unquote(path).lower(), (404, {}, {})))
        self.addCleanup(self.server.close)
        self.provider = GitHub(HTTP(self.server.url, "pat-fixture", test_loopback=True), self.store)

    def actor(self, kind, login="fixture-app"):
        return self.provider.actor({"author": {"login": login, "__typename": kind}})

    def test_Bot_and_colliding_User_are_verified_and_cached_separately_in_either_order(self):
        for order in (("User", "Bot"), ("Bot", "User")):
            with self.subTest(order=order):
                self.provider.users.clear()
                self.server.requests.clear()
                actors = {kind: self.actor(kind) for kind in order}
                self.assertEqual(actors["Bot"]["platform_actor_id"], "901")
                self.assertTrue(actors["Bot"]["is_bot"])
                self.assertEqual(actors["Bot"]["username"], "fixture-app[bot]")
                self.assertEqual(actors["User"]["platform_actor_id"], "903")
                self.assertFalse(actors["User"]["is_bot"])
                for kind in order:
                    self.assertEqual(self.actor(kind, "FIXTURE-APP"), actors[kind])
                self.assertCountEqual([r[1] for r in self.server.requests],
                                      ["/users/fixture-app", "/users/fixture-app%5Bbot%5D"])

    def test_Bot_lookup_requires_matching_type_login_and_positive_integer_id(self):
        invalid = [{**self.bot, **changes} for changes in (
            {"type": "User"}, {"type": None}, {"login": "fixture-app"}, {"login": "other[bot]"},
            {"login": None}, {"id": "901"}, {"id": None}, {"id": True}, {"id": 0}, {"id": -1})] + [{}, []]
        for response in [(200, value, {}) for value in invalid] + [(404, {}, {}), (500, {}, {})]:
            with self.subTest(response=response):
                self.provider.users.clear()
                self.responses["/users/fixture-app[bot]"] = response
                actor = self.actor("Bot")
                self.assertEqual(actor.get("attribution"), "unknown")
                self.assertNotIn("platform_actor_id", actor)
                self.assertNotIn("is_bot", actor)
                self.assertFalse(self.provider.users)
                self.responses["/users/fixture-app[bot]"] = (200, self.bot, {})
                self.assertEqual(self.actor("Bot")["platform_actor_id"], "901")

    def test_only_explicit_Bot_typename_normalizes_and_legacy_suffix_is_not_doubled(self):
        self.assertEqual(self.actor("Bot", "fixture-app[bot]")["platform_actor_id"], "901")
        self.assertEqual(self.actor("User", "fixture-app[bot]").get("attribution"), "unknown")
        self.assertEqual(self.provider.actor({"author": {"login": "fixture-app"}})["platform_actor_id"], "903")
        self.assertEqual(self.actor("User")["platform_actor_id"], "903")
        self.assertNotIn("/users/fixture-app%5Bbot%5D%5Bbot%5D", [r[1] for r in self.server.requests])

    def test_REST_numeric_actors_and_PAT_identity_are_preserved_without_actor_lookup(self):
        for user in (self.human, self.bot):
            raw = {"user": copy.deepcopy(user), "body": "Original"}
            original = copy.deepcopy(raw)
            actor = self.provider.actor(raw)
            self.assertEqual(actor["platform_actor_id"], str(user["id"]))
            self.assertEqual(actor["is_bot"], user["type"] == "Bot")
            self.assertEqual(raw, original)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.provider.identity(903), self.human)
        self.assertEqual([r[1] for r in self.server.requests], ["/user"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
