"""
Textual commands for the command palette.
"""
from textual.command import Provider, Hit, DiscoveryHit

class SubstitutionsCommandProvider(Provider):
    async def discover(self):
        if hasattr(self.screen.app, "_current_config_name") and self.screen.app._current_config_name:
            yield DiscoveryHit("Save Substitutions Config", lambda app=self.screen.app: app.action_save_subs(), help=f"Save substitutions to {self.screen.app._current_config_name}")
        yield DiscoveryHit("Save Substitutions Config As...", lambda app=self.screen.app: app.action_save_subs_as(), help="Save current substitutions to disk with a new name")
        yield DiscoveryHit("Load Substitutions Config", lambda app=self.screen.app: app.action_load_subs(), help="Load saved substitutions from disk")

    async def search(self, query: str):
        matcher = self.matcher(query)
        if hasattr(self.screen.app, "_current_config_name") and self.screen.app._current_config_name:
            match0 = matcher.match("Save Substitutions Config")
            if match0 > 0:
                yield Hit(match0, matcher.highlight("Save Substitutions Config"), lambda app=self.screen.app: app.action_save_subs(), help=f"Save substitutions to {self.screen.app._current_config_name}")

        match1 = matcher.match("Save Substitutions Config As...")
        if match1 > 0:
            yield Hit(match1, matcher.highlight("Save Substitutions Config As..."), lambda app=self.screen.app: app.action_save_subs_as(), help="Save current substitutions to disk with a new name")

        match2 = matcher.match("Load Substitutions Config")
        if match2 > 0:
            yield Hit(match2, matcher.highlight("Load Substitutions Config"), lambda app=self.screen.app: app.action_load_subs(), help="Load saved substitutions from disk")
