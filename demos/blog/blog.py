#!/usr/bin/env python3
#
# Copyright 2009 Facebook
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import aiopg
import asyncio
import bcrypt
import markdown
import os.path
import psycopg2
import re
import tornado
import unicodedata
import sys
import asyncpg

from tornado.options import define, options

define("port", default=8888, help="run on the given port", type=int)
define("db_host", default="127.0.0.1", help="blog database host")
define("db_port", default=5432, help="blog database port")
define("db_database", default="blog", help="blog database name")
define("db_user", default="postgres", help="blog database user")
define("db_password", default="123456", help="blog database password")


class NoResultError(Exception):
    pass


async def maybe_create_tables(db):
    try:
        await db.fetchval("SELECT COUNT(*) FROM entries LIMIT 1")
    except:
        with open("schema.sql") as f:
            schema = f.read()
        await db.execute(schema)


class Application(tornado.web.Application):
    def __init__(self, db):
        self.db = db
        handlers = [
            (r"/", HomeHandler),
            (r"/archive", ArchiveHandler),
            (r"/feed", FeedHandler),
            (r"/entry/([^/]+)", EntryHandler),
            (r"/compose", ComposeHandler),
            (r"/auth/create", AuthCreateHandler),
            (r"/auth/login", AuthLoginHandler),
            (r"/auth/logout", AuthLogoutHandler),
        ]
        settings = dict(
            blog_title="Tornado Blog",
            template_path=os.path.join(os.path.dirname(__file__), "templates"),
            static_path=os.path.join(os.path.dirname(__file__), "static"),
            ui_modules={"Entry": EntryModule},
            xsrf_cookies=True,
            cookie_secret="__TODO:_GENERATE_YOUR_OWN_RANDOM_VALUE_HERE__",
            login_url="/auth/login",
            debug=True,
        )
        super().__init__(handlers, **settings)


class BaseHandler(tornado.web.RequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._current_user = None

    def row_to_obj(self, row):
        """Convert a SQL row to an object supporting dict and attribute access."""
        obj = tornado.util.ObjectDict()
        for key, value in row.items():
            obj[key] = value
        return obj

    async def prepare(self):
        # get_current_user cannot be a coroutine, so set
        # self.current_user in prepare instead.
        user_id = self.get_signed_cookie("blogdemo_user")
        if user_id:
            try:
                row = await self.queryone(
                    "SELECT * FROM authors WHERE id = $1", int(user_id)
                )
                self._current_user = self.row_to_obj(row)
            except NoResultError:
                self._current_user = None

    async def execute(self, stmt, *args):
        await self.application.db.execute(stmt, *args)

    async def query(self, stmt, *args):
        rows = await self.application.db.fetch(stmt, *args)
        return [self.row_to_obj(row) for row in rows]

    async def queryone(self, stmt, *args):
        row = await self.application.db.fetchrow(stmt, *args)
        if not row:
            raise NoResultError()
        return dict(row)  # 保持为字典，在需要时转换为 ObjectDict

    async def any_author_exists(self):
        return bool(await self.query("SELECT * FROM authors LIMIT 1"))

    def get_current_user(self):
        return self._current_user


class HomeHandler(BaseHandler):
    async def get(self):
        entries = await self.query(
            "SELECT * FROM entries ORDER BY published DESC LIMIT 5"
        )
        if not entries:
            self.redirect("/compose")
            return
        self.render("home.html", entries=entries)


class EntryHandler(BaseHandler):
    async def get(self, slug):
        try:
            row = await self.queryone("SELECT * FROM entries WHERE slug = $1", slug)
            entry = self.row_to_obj(row)
            self.render("entry.html", entry=entry)
        except NoResultError:
            raise tornado.web.HTTPError(404)


class ArchiveHandler(BaseHandler):
    async def get(self):
        entries = await self.query("SELECT * FROM entries ORDER BY published DESC")
        self.render("archive.html", entries=entries)


class FeedHandler(BaseHandler):
    async def get(self):
        entries = await self.query(
            "SELECT * FROM entries ORDER BY published DESC LIMIT 10"
        )
        self.set_header("Content-Type", "application/atom+xml")
        self.render("feed.xml", entries=entries)


class ComposeHandler(BaseHandler):
    @tornado.web.authenticated
    async def get(self):
        id = self.get_argument("id", None)
        entry = None
        if id:
            try:
                row = await self.queryone("SELECT * FROM entries WHERE id = $1", int(id))
                entry = self.row_to_obj(row)
            except NoResultError:
                raise tornado.web.HTTPError(404)
        self.render("compose.html", entry=entry)

    @tornado.web.authenticated
    async def post(self):
        id = self.get_argument("id", None)
        title = self.get_argument("title")
        text = self.get_argument("markdown")
        html = markdown.markdown(text)

        if id:
            try:
                row = await self.queryone(
                    "SELECT * FROM entries WHERE id = $1", int(id)
                )
                entry = self.row_to_obj(row)
            except NoResultError:
                raise tornado.web.HTTPError(404)
            slug = entry.slug
            await self.execute(
                "UPDATE entries SET title = $1, markdown = $2, html = $3 "
                "WHERE id = $4",
                title,
                text,
                html,
                int(id),
            )
        else:
            slug = unicodedata.normalize("NFKD", title)
            slug = re.sub(r"[^\w]+", " ", slug)
            slug = "-".join(slug.lower().strip().split())
            slug = slug.encode("ascii", "ignore").decode("ascii")
            if not slug:
                slug = "entry"
            while True:
                e = await self.query("SELECT * FROM entries WHERE slug = $1", slug)
                if not e:
                    break
                slug += "-2"
            await self.execute(
                "INSERT INTO entries (author_id,title,slug,markdown,html,published,updated)"
                "VALUES ($1,$2,$3,$4,$5,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                self.current_user.id,
                title,
                slug,
                text,
                html,
            )
        self.redirect("/entry/" + slug)


class AuthCreateHandler(BaseHandler):
    def get(self):
        self.render("create_author.html")

    async def post(self):
        if await self.any_author_exists():
            raise tornado.web.HTTPError(400, "author already created")
        hashed_password = await tornado.ioloop.IOLoop.current().run_in_executor(
            None,
            bcrypt.hashpw,
            tornado.escape.utf8(self.get_argument("password")),
            bcrypt.gensalt(),
        )
        row = await self.queryone(
            "INSERT INTO authors (email, name, hashed_password) "
            "VALUES ($1, $2, $3) RETURNING id",
            self.get_argument("email"),
            self.get_argument("name"),
            tornado.escape.to_unicode(hashed_password),
        )
        author = self.row_to_obj(row)
        self.set_signed_cookie("blogdemo_user", str(author.id))
        self.redirect(self.get_argument("next", "/"))


class AuthLoginHandler(BaseHandler):
    async def get(self):
        # If there are no authors, redirect to the account creation page.
        if not await self.any_author_exists():
            self.redirect("/auth/create")
        else:
            self.render("login.html", error=None)

    async def post(self):
        try:
            row = await self.queryone(
                "SELECT * FROM authors WHERE email = $1", self.get_argument("email")
            )
            author = self.row_to_obj(row)
        except NoResultError:
            self.render("login.html", error="email not found")
            return

        password_equal = await tornado.ioloop.IOLoop.current().run_in_executor(
            None,
            bcrypt.checkpw,
            tornado.escape.utf8(self.get_argument("password")),
            tornado.escape.utf8(author.hashed_password),
        )
        if password_equal:
            self.set_signed_cookie("blogdemo_user", str(author.id))
            self.redirect(self.get_argument("next", "/"))
        else:
            self.render("login.html", error="incorrect password")


class AuthLogoutHandler(BaseHandler):
    def get(self):
        self.clear_cookie("blogdemo_user")
        self.redirect(self.get_argument("next", "/"))


class EntryModule(tornado.web.UIModule):
    def render(self, entry):
        return self.render_string("modules/entry.html", entry=entry)


async def main():
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    tornado.options.parse_command_line()

    # 使用asyncpg替代aiopg
    conn = await asyncpg.connect(
        host=options.db_host,
        port=options.db_port,
        user=options.db_user,
        password=options.db_password,
        database=options.db_database,
    )

    try:
        await maybe_create_tables(conn)
        app = Application(conn)
        app.listen(options.port)
        print(f"Server started on http://localhost:{options.port}")

        # 保持服务器运行
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        print("Server stopped")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())