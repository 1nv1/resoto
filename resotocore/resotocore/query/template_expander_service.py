from typing import Optional, List

from resotocore.cli import strip_quotes
from resotocore.cli.model import CLI, CLIContext
from resotocore.db.templatedb import TemplateEntityDb
from resotocore.query.model import Template
from resotocore.query.template_expander import TemplateExpanderBase
from resotocore.service import Service
from resotocore.types import Json


class TemplateExpanderService(TemplateExpanderBase, Service):
    """
    Template expander, which maintains the templates in the database.
    """

    def __init__(self, db: TemplateEntityDb, cli: CLI) -> None:
        self.db = db
        self.cli = cli

    def default_props(self) -> Optional[Json]:
        return None

    async def put_template(self, template: Template) -> None:
        await self.db.update(template)

    async def delete_template(self, name: str) -> None:
        await self.db.delete(name)

    async def get_template(self, name: str) -> Optional[Template]:
        return await self.db.get(name)

    async def list_templates(self) -> List[Template]:
        return [t async for t in self.db.all()]

    async def parse_query_from_command_line(self, to_parse: str, on_section: str, **env: str) -> str:
        final_env = {**env, "section": on_section, "no_rewrite": "true"}
        parsed = await self.cli.evaluate_cli_command(to_parse, CLIContext(env=final_env), replace_place_holder=False)
        if len(parsed) == 1:
            first_line = parsed[0]
            if len(first_line.executable_commands) == 1 and first_line.executable_commands[0].name == "execute_search":
                query = first_line.executable_commands[0].arg or ""
                return strip_quotes(query)
            else:
                iv = ", ".join(cmd.name for cmd in first_line.executable_commands[1:])
                raise ValueError(f"Commands found that can not be used as part of search: {iv}")
        else:
            raise ValueError("Multiple command lines are not supported in search!")
