"""Lifespan orchestration for L4 FastAPI startup and shutdown ordering."""

from __future__ import annotations

from contextlib import asynccontextmanager


def build_lifespan(runtime, comments, account, dynamics, messages, browsing, store, release_writer):
    """Create the FastAPI lifespan context manager closing services in reverse startup order."""

    @asynccontextmanager
    async def lifespan(_app):
        try:
            await runtime.open()
            await comments.open()
            await account.open()
            await dynamics.open()
            await messages.open()
            await browsing.open()
            runtime.memory_organizer.start()
            runtime.tasks.start()
            yield
        finally:
            try:
                try:
                    await runtime.tasks.close()
                finally:
                    await runtime.memory_organizer.close()
                    await browsing.close()
            finally:
                try:
                    await messages.close()
                finally:
                    try:
                        await dynamics.close()
                    finally:
                        try:
                            try:
                                await account.close()
                            finally:
                                await comments.close()
                        finally:
                            try:
                                await runtime.close()
                            finally:
                                try:
                                    store.close()
                                finally:
                                    release_writer()

    return lifespan
