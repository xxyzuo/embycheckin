import asyncio
import random
import time
from typing import Callable, Coroutine, List, Optional, Tuple, Union
import uuid

import tomli
from loguru import logger
from pyrogram import filters
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message
from pyrogram.raw.functions.messages import DeleteHistory

from ..utils import async_partial, truncate_str
from .tele import Client


class Link:
    """云服务类, 用于认证和高级权限任务通讯."""

    bot = "embykeeper_auth_bot"

    def __init__(self, client: Client):
        self.client = client
        self.log = logger.bind(scheme="telelink", username=client.me.name)

    @property
    def instance(self):
        """当前设备识别码."""
        rd = random.Random()
        rd.seed(uuid.getnode())
        return uuid.UUID(int=rd.getrandbits(128))

    async def delete_messages(self, messages: List[Message]):
        """删除一系列消息."""

        async def delete(m: Message):
            try:
                await m.delete(revoke=True)
                text = m.text or m.caption or "图片或其他内容"
                text = truncate_str(text.replace("\n", ""), 30)
                self.log.debug(f"[gray50]删除了 API 消息记录: {text}[/]")
            except asyncio.CancelledError:
                pass

        return await asyncio.gather(*[delete(m) for m in messages])

    async def post(
        self, cmd, condition: Callable = None, timeout: int = 20, retries=3, name: str = None
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        向机器人发送请求.
        参数:
            cmd: 命令字符串
            condition: 布尔或函数, 参数为响应 toml 的字典形式, 决定该响应是否为有效响应.
            timeout: 超时 (s)
            retries: 最大重试次数
            name: 请求名称, 用于用户提示
        """
        for r in range(retries):
            self.log.debug(f"[gray50]禁用提醒 {timeout} 秒: {self.bot}[/]")
            await self.client.mute_chat(self.bot, time.time() + timeout + 5)
            future = asyncio.Future()
            handler = MessageHandler(
                async_partial(self._handler, cmd=cmd, future=future, condition=condition),
                filters.text & filters.bot & filters.user(self.bot),
            )
            await self.client.add_handler(handler, group=1)
            try:
                messages = []
                messages.append(await self.client.send_message(self.bot, f"/start quiet"))
                await asyncio.sleep(0.5)
                messages.append(await self.client.send_message(self.bot, cmd))
                self.log.debug(f"[gray50]-> {cmd}[/]")
                results = await asyncio.wait_for(future, timeout=timeout)
            except asyncio.CancelledError:
                try:
                    await asyncio.wait_for(self.delete_messages(messages), 1.0)
                except asyncio.TimeoutError:
                    pass
                finally:
                    raise
            except asyncio.TimeoutError:
                await self.delete_messages(messages)
                if r + 1 < retries:
                    self.log.info(f"{name}超时 ({r + 1}/{retries}), 将在 3 秒后重试.")
                    await asyncio.sleep(3)
                    continue
                else:
                    self.log.warning(f"{name}超时 ({r + 1}/{retries}).")
                    return None
            else:
                await self.delete_messages(messages)
                status, errmsg = [results.get(p, None) for p in ("status", "errmsg")]
                if status == "error":
                    self.log.warning(f"{name}错误: {errmsg}.")
                    return False
                elif status == "ok":
                    return results
                else:
                    self.log.warning(f"{name}出现未知错误.")
                    return False
            finally:
                await self.client.remove_handler(handler, group=1)

    async def _handler(
        self,
        client: Client,
        message: Message,
        cmd: str,
        future: asyncio.Future,
        condition: Union[bool, Callable[..., Coroutine], Callable] = None,
    ):
        try:
            toml = tomli.loads(message.text)
        except tomli.TOMLDecodeError:
            self.delete_messages([message])
        else:
            try:
                if toml.get("command", None) == cmd:
                    if condition is None:
                        cond = True
                    elif asyncio.iscoroutinefunction(condition):
                        cond = await condition(toml)
                    elif callable(condition):
                        cond = condition(toml)
                    if cond:
                        future.set_result(toml)
                        await asyncio.sleep(0.5)
                        await self.delete_messages([message])
                        return
            except asyncio.CancelledError as e:
                try:
                    await asyncio.wait_for(self.delete_messages([message]), 1)
                except asyncio.TimeoutError:
                    pass
                finally:
                    future.set_exception(e)
            finally:
                message.continue_propagation()

    async def delete_history(self):
        self.log.debug("清空机器人日志记录.")
        await self.client.invoke(DeleteHistory(max_id=0, peer=await self.client.resolve_peer(self.bot)))

    async def auth(self, service: str):
        """向机器人发送授权请求."""
        results = await self.post(f"/auth {service} {self.instance}", name=f"服务 {service.upper()} 认证")
        return bool(results)

    async def captcha(self):
        """向机器人发送 Cloudflare 验证码解析请求."""
        results = await self.post(f"/captcha {self.instance}", timeout=240, name="请求跳过验证码")
        if results:
            return [results.get(p, None) for p in ("token", "proxy", "useragent")]
        else:
            return None, None, None

    async def answer(self, question: str):
        """向机器人发送问题回答请求."""
        results = await self.post(f"/answer {self.instance} {question}", timeout=20, name="请求问题回答")
        if results:
            return results.get("answer", None), results.get("by", None)
        else:
            return None, None

    async def gpt(self, prompt: str):
        """向机器人发送智能回答请求."""
        results = await self.post(f"/gpt {self.instance} {prompt}", timeout=20, name="请求智能回答")
        if results:
            return results.get("answer", None), results.get("by", None)
        else:
            return None, None

    async def send_log(self, message):
        """向机器人发送日志记录请求."""
        results = await self.post(f"/log {self.instance} {message}", name="发送日志到 Telegram")
        return bool(results)
