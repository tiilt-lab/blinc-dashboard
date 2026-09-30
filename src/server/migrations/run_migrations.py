"""Run the Alembic chain against an empty database without the Flask app.

CI proves the chain can rebuild production from nothing: upgrade head,
downgrade base, upgrade head again. Importing app.py would need config.ini,
Redis and the full server dependency set, so this builds the Alembic config
directly and env.py falls back to the URL set here when there is no Flask
application context.

    DC_DATABASE_URL=mysql+mysqlconnector://user:pw@host:3306/db \\
        python src/server/migrations/run_migrations.py [upgrade|roundtrip]

Without DC_DATABASE_URL the URL is assembled from DC_DATABASE_USER (also the
password, as in config.database_user()), DC_DATABASE_HOST and
DC_DATABASE_NAME.
"""
import os
import sys

from alembic import command
from alembic.config import Config

HERE = os.path.dirname(os.path.abspath(__file__))


def database_url():
    url = os.environ.get('DC_DATABASE_URL')
    if url:
        return url
    user = os.environ.get('DC_DATABASE_USER', 'vagrant')
    host = os.environ.get('DC_DATABASE_HOST', 'localhost:3306')
    name = os.environ.get('DC_DATABASE_NAME', 'discussion_capture')
    return 'mysql+mysqlconnector://{0}:{0}@{1}/{2}'.format(user, host, name)


def alembic_config():
    cfg = Config(os.path.join(HERE, 'alembic.ini'))
    cfg.set_main_option('script_location', HERE)
    cfg.set_main_option('sqlalchemy.url', database_url().replace('%', '%%'))
    return cfg


def main(argv):
    mode = argv[1] if len(argv) > 1 else 'roundtrip'
    cfg = alembic_config()
    command.upgrade(cfg, 'head')
    if mode == 'roundtrip':
        command.downgrade(cfg, 'base')
        command.upgrade(cfg, 'head')
    elif mode != 'upgrade':
        raise SystemExit('usage: run_migrations.py [upgrade|roundtrip]')


if __name__ == '__main__':
    main(sys.argv)
