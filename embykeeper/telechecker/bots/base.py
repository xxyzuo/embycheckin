import asyncio
from pathlib import Path
import random
import re
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from enum import Flag, auto
import string
import time
from typing import Iterable, List, Union

from ddddocr import DdddOcr
from appdirs import user_data_dir
from loguru import logger
from PIL import Image
from pyrogram import filters
from pyrogram.errors import UsernameNotOccupied, FloodWait
from pyrogram.handlers import EditedMessageHandler, MessageHandler
from pyrogram.types import InlineKeyboardMarkup, Message, ReplyKeyboardMarkup
from onnxruntime.capi.onnxruntime_pybind11_state import InvalidProtobuf
from thefuzz import fuzz, process

from ...data import get_datas
from ...utils import show_exception, to_iterable, AsyncCountPool, truncate_str
from ..lock import ocrs, ocrs_lock
from ..tele import Client
from ..link import Link

__ignore__ = True

default_keywords = {
    "account_fail": (
        "拉黑",
        "黑名单",
        "冻结",
        "未找到用户",
        "无资格",
        "退出群",
        "退群",
        "加群",
        "加入群聊",
        "请先关注",
        "请先加入",
        "注册",
        "不存在",
    ),
    "too_many_tries_fail": ("已尝试", "过多"),
    "checked": ("只能", "已经", "下次", "过了", "签过", "明日再来", "上次签到"),
    "fail": ("失败", "错误", "超时"),
    "success": ("成功", "通过", "完成", "获得"),
}


class MessageType(Flag):
    TEXT = auto()
    CAPTION = auto()
    CAPTCHA = auto()
    ANSWER = auto()


class BaseBotCheckin(ABC):
    """基础签到类."""

    name = None

    def __init__(
        self, client: Client, retries=4, timeout=120, nofail=True, basedir=None, proxy=None, config: dict = {}
    ):
        """
        基础签到类.
        参数:
            client: Pyrogram 客户端
            retries: 最大重试次数
            timeout: 签到超时时间
            nofail: 启用错误处理外壳, 当错误时报错但不退出
            basedir: 文件存储默认位置
            proxy: 代理配置
            config: 当前签到器的特定配置
        """
        self.client = client
        self.retries = retries
        self.timeout = timeout
        self.nofail = nofail
        self.basedir = basedir or user_data_dir(__name__)
        self.proxy = proxy
        self.config = config
        self.finished = asyncio.Event()  # 签到完成事件
        self.log = logger.bind(scheme="telechecker", name=self.name, username=client.me.name)  # 日志组件

    async def _start(self):
        """签到器的入口函数的错误处理外壳."""
        try:
            return await self.start()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if self.nofail:
                self.log.warning(f"初始化错误, 签到器将停止.")
                show_exception(e, regular=False)
                return False
            else:
                raise

    @abstractmethod
    async def start(self):
        pass


class BotCheckin(BaseBotCheckin):
    """签到类, 用于回复模式签到."""

    group_pool = AsyncCountPool(base=2000)
    ocr = None

    name: str = None  # 签到器的名称
    bot_username: Union[int, str] = None  # Bot 的 UserID 或 用户名 (不带 @ 或 https://t.me/)
    bot_checkin_cmd: Union[str, List[str]] = ["/checkin"]  # Bot 依次执行的签到命令
    bot_send_interval: int = 1  # 签到命令间等待的秒数
    bot_checkin_caption_pat: str = None  # 当 Bot 返回图片时, 仅当符合该 regex 才识别为验证码, 置空不限制
    bot_text_ignore: Union[str, List[str]] = []  # 当含有列表中的关键词, 即忽略该消息, 置空不限制
    bot_captcha_len: Union[int, Iterable[int]] = []  # 验证码的可能范围, 例如 [1, 2, 3], 置空不限制
    bot_success_pat: str = r"(\d+)[^\d]*(\d+)"  # 当接收到成功消息后, 从消息中提取数字的模式
    bot_retry_wait: int = 2  # 失败时等待的秒数
    bot_use_history: int = None  # 首先尝试识别历史记录中最后一个验证码图片, 最多识别 N 条, 置空禁用
    bot_allow_from_scratch: bool = False  # 允许从未聊天情况下启动
    bot_success_keywords: Union[str, List[str]] = []  # 成功时检测的关键词 (暂不支持regex), 置空使用内置关键词表
    bot_checked_keywords: Union[str, List[str]] = []  # 今日已签到时检测的关键词, 置空使用内置关键词表
    bot_account_fail_keywords: Union[str, List[str]] = []  # 账户错误将退出时检测的关键词 (暂不支持regex), 置空使用内置关键词表
    bot_too_many_tries_fail_keywords: Union[str, List[str]] = []  # 账户错误将退出时检测的关键词 (暂不支持regex), 置空使用内置关键词表
    bot_fail_keywords: Union[str, List[str]] = []  # 签到错误将重试时检测的关键词 (暂不支持regex), 置空使用内置关键词表
    chat_name: str = None  # 在群聊中向机器人签到
    additional_auth: List[str] = []  # 额外认证要求

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._is_archived = False  # 签到前处于收纳状态
        self._retries = 0  # 当前重试次数
        self._waiting = {}  # 当前等待的消息

    def get_filter(self):
        """设定要签到的目标."""
        filter = filters.user(self.bot_username)
        if self.chat_name:
            filter = filter & filters.chat(self.chat_name)
        else:
            filter = filter & filters.private
        return filter

    def get_handlers(self):
        """设定要监控的更新的类型."""
        return [
            MessageHandler(self._message_handler, self.get_filter()),
            EditedMessageHandler(self._message_handler, self.get_filter()),
        ]

    @asynccontextmanager
    async def listener(self):
        """执行监控上下文."""
        group = await self.group_pool.append(self)
        handlers = self.get_handlers()
        for h in handlers:
            self.client.add_handler(h, group=group)
        yield
        for h in handlers:
            try:
                self.client.remove_handler(h, group=group)
            except ValueError:
                pass

    async def get_ocr(self, ocr: str = None):
        """加载特定标签的 OCR 模型, 默认加载 ddddocr 默认模型."""
        while True:
            async with ocrs_lock:
                if ocr in ocrs:
                    return ocrs[ocr]
                if not ocr:
                    model = DdddOcr(beta=True, show_ad=False)
                else:
                    data = []
                    files = (f"{ocr}.onnx", f"{ocr}.json")
                    async for p in get_datas(self.basedir, files, proxy=self.proxy, caller=self.name):
                        if p is None:
                            self.log.warning(f"初始化错误: 无法下载所需文件.")
                            return None
                        else:
                            data.append(p)
                    try:
                        model = DdddOcr(
                            show_ad=False, import_onnx_path=str(data[0]), charsets_path=str(data[1])
                        )
                    except InvalidProtobuf:
                        self.log.warning(f"文件下载不完全, 正在重试下载.")
                        Path(str(data[0])).unlink()
                        Path(str(data[1])).unlink()
                        continue
                ocrs[ocr] = model
                return model

    async def start(self):
        """签到器的入口函数."""
        ident = self.chat_name or self.bot_username
        while True:
            try:
                chat = await self.client.get_chat(ident)
            except UsernameNotOccupied:
                self.log.warning(f'初始化错误: 会话 "{ident}" 不存在.')
                return False
            except KeyError as e:
                self.log.info(f"初始化错误: 无法访问, 您可能已被封禁: {e}.")
                show_exception(e)
                return False
            except FloodWait as e:
                self.log.info(f"初始化信息: Telegram 要求等待 {e.value} 秒.")
                if e.value < 360:
                    await asyncio.sleep(e.value)
                else:
                    self.log.info(f"初始化信息: Telegram 要求等待 {e.value} 秒, 您可能操作过于频繁, 签到器将停止.")
                    return False
            else:
                break
        async for d in self.client.get_dialogs(folder_id=1):
            if d.chat.id == chat.id:
                self._is_archived = True
                break
        else:
            async for d in self.client.get_dialogs(folder_id=0):
                if d.chat.id == chat.id:
                    break
            else:
                if not self.bot_allow_from_scratch:
                    self.log.info(f'跳过签到: 从未与 "{ident}" 交流.')
                    return None

        if not await self.init():
            self.log.warning(f"初始化错误.")
            return False

        if self.additional_auth:
            for a in self.additional_auth:
                if not await Link(self.client).auth(a):
                    self.log.info(f"初始化错误: 权限校验不通过, 需要: {a}.")
                    return False

        bot = await self.client.get_users(self.bot_username)
        msg = f"开始执行签到: [green]{bot.name}[/] [gray50](@{bot.username})[/]"
        if chat.title:
            msg += f" @ [green]{chat.title}[/] [gray50](@{chat.username})[/]"
        self.log.info(msg + ".")

        if not self.chat_name:
            self.log.debug(f"[gray50]禁用提醒 {self.timeout} 秒: {bot.username}[/]")
            await self.client.mute_chat(ident, time.time() + self.timeout + 10)

        try:
            async with self.listener():
                cancelled = False
                try:
                    if self.bot_use_history is None:
                        await self.send_checkin()
                    elif not await self.walk_history(self.bot_use_history):
                        await self.send_checkin()
                    await asyncio.wait_for(self.finished.wait(), self.timeout)
                except asyncio.CancelledError:
                    cancelled = True
                    raise
                finally:
                    if not cancelled:
                        if not await self.cleanup():
                            self.log.debug(f"[gray50]执行清理失败: {ident}[/]")
                        if self._is_archived:
                            self.log.debug(f"[gray50]将会话重新归档: {ident}[/]")
                            try:
                                if await chat.archive():
                                    self.log.debug(f"[gray50]重新归档成功: {ident}[/]")
                            except asyncio.TimeoutError:
                                self.log.debug(f"[gray50]归档失败: {ident}[/]")
                        if not self.chat_name:
                            self.log.debug(f"[gray50]将会话设为已读: {ident}[/]")
                            try:
                                if await self.client.read_chat_history(ident):
                                    self.log.debug(f"[gray50]设为已读成功: {ident}[/]")
                            except asyncio.TimeoutError:
                                self.log.debug(f"[gray50]设为已读失败: {ident}[/]")
        except OSError as e:
            self.log.warning(f'初始化错误: "{e}", 签到器将停止.')
            show_exception(e)
            return False
        except asyncio.TimeoutError:
            pass
        if not self.finished.is_set():
            self.log.warning("无法在时限内完成签到.")
            return False
        else:
            return self._retries <= self.retries

    async def init(self):
        """可重写的初始化函数, 在读取聊天后运行, 在执行签到前运行, 返回 False 将视为初始化错误."""
        return True

    async def cleanup(self):
        """可重写的签到结束后的清理函数, 执行签到成功或失败后运行, 返回 False 将视为清理错误."""
        return True

    async def walk_history(self, limit=0):
        """处理 limit 条历史消息, 并检测是否有验证码."""
        try:
            async for m in self.client.get_chat_history(self.chat_name or self.bot_username, limit=limit):
                if MessageType.CAPTCHA in self.message_type(m):
                    await self.on_photo(m)
                    return True
            return False
        except Exception as e:
            self.log.warning("读取历史消息失败, 将不再读取历史消息.")
            show_exception(e)
            return False

    async def send(self, cmd):
        """向机器人发送命令."""
        if self.chat_name:
            bot = await self.client.get_users(self.bot_username)
            await self.client.send_message(self.chat_name, f"{cmd}@{bot.username}")
        else:
            await self.client.send_message(self.bot_username, cmd)

    async def send_checkin(self, retry=False):
        """发送签到命令, 或依次发送签到命令序列."""
        cmds = to_iterable(self.bot_checkin_cmd)
        for i, cmd in enumerate(cmds):
            if retry and not i:
                await asyncio.sleep(self.bot_retry_wait)
            if i < len(cmds):
                await asyncio.sleep(self.bot_send_interval)
            await self.send(cmd)

    async def _message_handler(self, client: Client, message: Message):
        """消息处理入口函数的错误处理外壳."""
        try:
            await self.message_handler(client, message)
        except OSError as e:
            self.log.info(f'发生错误: "{e}", 正在重试.')
            await self.retry()
            message.continue_propagation()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not self.nofail:
                await self.fail()
                raise
            else:
                self.log.warning(f"发生错误, 签到器将停止.")
                show_exception(e, regular=False)
                await self.fail()
                message.continue_propagation()
        else:
            message.continue_propagation()

    async def message_handler(self, client: Client, message: Message, type=None):
        """消息处理入口函数."""
        text = message.text or message.caption
        if text:
            for p, k in self._waiting.items():
                if re.search(p, text):
                    k.set()
                    self._waiting[p] = message
        type = type or self.message_type(message)
        if MessageType.TEXT in type:
            await self.on_text(message, message.text)
        if MessageType.CAPTION in type:
            await self.on_text(message, message.caption)
        if MessageType.CAPTCHA in type:
            await self.on_photo(message)

    def message_type(self, message: Message):
        """分析传入消息的类型为验证码或文字."""
        if message.photo:
            if message.caption:
                if self.bot_checkin_caption_pat:
                    if re.search(self.bot_checkin_caption_pat, message.caption):
                        return MessageType.CAPTCHA
                    else:
                        return MessageType.CAPTION
                else:
                    return MessageType.CAPTCHA
            else:
                return MessageType.CAPTCHA
        elif message.text:
            return MessageType.TEXT

    async def on_photo(self, message: Message):
        """分析分析传入的验证码图片并返回验证码."""
        data = await self.client.download_media(message, in_memory=True)
        image = Image.open(data)
        ocr = await self.get_ocr(self.ocr)
        captcha = (
            ocr.classification(image).translate(str.maketrans("", "", string.punctuation)).replace(" ", "")
        )
        if captcha:
            self.log.debug(f"[gray50]接收验证码: {captcha}.[/]")
            if self.bot_captcha_len and len(captcha) not in to_iterable(self.bot_captcha_len):
                self.log.info(f"签到失败: 验证码低于设定长度, 正在重试.")
                await self.retry()
            else:
                await asyncio.sleep(random.uniform(2, 4))
                await self.on_captcha(message, captcha)
        else:
            self.log.info(f"签到失败: 接收到空验证码, 正在重试.")
            await self.retry()

    async def on_captcha(self, message: Message, captcha: str):
        """
        可修改的回调函数.
            message: 包含验证码图片的消息
            captcha: OCR 识别的验证码
        """
        await message.reply(captcha)

    async def on_text(self, message: Message, text: str):
        """接收非验证码消息时, 检测关键词并确认签到成功或失败, 发送用户提示."""
        if any(s in text for s in to_iterable(self.bot_text_ignore)):
            pass
        elif any(s in text for s in self.bot_account_fail_keywords or default_keywords["account_fail"]):
            self.log.warning(f"签到失败: 账户错误.")
            await self.fail()
        elif any(
            s in text
            for s in self.bot_too_many_tries_fail_keywords or default_keywords["too_many_tries_fail"]
        ):
            self.log.warning(f"签到失败: 尝试次数过多.")
            await self.fail()
        elif any(s in text for s in self.bot_checked_keywords or default_keywords["checked"]):
            self.log.info(f"今日已经签到过了.")
            self.finished.set()
        elif any(s in text for s in self.bot_fail_keywords or default_keywords["fail"]):
            self.log.info(f"签到失败: 验证码错误, 正在重试.")
            await self.retry()
        elif any(s in text for s in self.bot_success_keywords or default_keywords["success"]):
            matches = re.search(self.bot_success_pat, text)
            if matches:
                try:
                    self.log.info(f"[yellow]签到成功[/]: + {matches.group(1)} 分 -> {matches.group(2)} 分.")
                except IndexError:
                    self.log.info(f"[yellow]签到成功[/]: 当前/增加 {matches.group(1)} 分.")
            else:
                matches = re.search(r"\d+", text)
                if matches:
                    self.log.info(f"[yellow]签到成功[/]: 当前/增加 {matches.group(0)} 分.")
                else:
                    self.log.info(f"[yellow]签到成功[/].")
            self.finished.set()
        else:
            await self.on_unexpected_text(message)

    async def on_unexpected_text(self, message: Message):
        content = message.text or message.caption
        if content:
            spec = content.replace("\n", " ")
            self.log.warning(f"接收到异常返回信息: {spec}, 正在尝试智能回答.")
            if message.reply_markup and message.reply_markup.inline_keyboard:
                buttons = [b.text for r in message.reply_markup.inline_keyboard for b in r]
            else:
                buttons = []
            button_specs = [f"'{b}'" for b in buttons]
            prompt = (
                "我正在进行签到, 机器将显示指令或状态, 我需要通过回答问题以完成签到, 现在机器给出的值为:\n\n"
                f"{content}\n\n"
                f"你可选: {', '.join(button_specs)} 中的一个作为回答.\n"
                "如果您认为不应该进行任何操作, 请输出 'NO_RESP'.\n"
                "如果这是一个指令, 请输出您需要发送或点击的内容, 不要说明这是一个指令, 不要说明需要发送文本消息, 仅仅输出发送或点击的内容. "
                "如果这是一个状态, 请输出 'IS_STATUS', 禁止输出其他内容."
            )
            for _ in range(3):
                answer, by = await Link(self.client).gpt(prompt)
                if answer:
                    self.log.debug(f"智能回答 ({by}): {answer}")
                    if "NO_RESP" in answer:
                        self.log.info(f"智能回答认为无需进行操作.")
                        return
                    if "IS_STATUS" in answer:
                        self.log.info(f"智能回答认为这是一条状态信息.")
                        return
                    if buttons:
                        self.log.debug(f"当前按钮: {', '.join(button_specs)}")
                        b, s = process.extractOne(answer, buttons, scorer=fuzz.partial_ratio)
                        if s < 70:
                            self.log.info(f"找不到对应回答的按钮, 正在重试.")
                            await asyncio.sleep(3)
                            continue
                        else:
                            try:
                                await message.click(b)
                            except TimeoutError:
                                pass
                            self.log.warning(f'智能回答点击了按钮 "{b}", 为了避免风险签到器将停止.')
                            await self.fail()
                            return
                    else:
                        await message.reply(answer)
                        self.log.warning(f'智能回答回复了 "{b}", 为了避免风险签到器将停止.')
                        await self.fail()
                        return
            else:
                self.log.warning(f"智能回答失败, 为了避免风险签到器将停止.")
                await self.fail()
                return

    async def retry(self):
        """执行重试, 重新发送签到指令."""
        self._retries += 1
        if self._retries <= self.retries:
            await asyncio.sleep(self.bot_retry_wait)
            await self.send_checkin(retry=True)
        else:
            self.log.warning("超过最大重试次数.")
            self.finished.set()

    async def fail(self):
        """设定签到器失败."""
        self.finished.set()
        self._retries = float("inf")

    async def wait_until(self, pattern: str = ".", timeout: float = None):
        """等待特定消息出现."""
        self._waiting[pattern] = e = asyncio.Event()
        try:
            await asyncio.wait_for(e.wait(), timeout)
        except asyncio.TimeoutError:
            return None
        else:
            msg: Message = self._waiting[pattern]
            return msg


class AnswerBotCheckin(BotCheckin):
    """签到类, 用于按钮模式签到."""

    bot_checkin_button_pat: str = None  # 所有按键需要满足的 regex 条件

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.mutex = asyncio.Lock()  # 实例变量异步锁
        self.operable = asyncio.Condition(self.mutex)  # 当 message 被设定时提醒等待的异步线程.
        self.message: Message = None  # 存储可用于回复的带按钮的消息.

    async def walk_history(self, limit=0):
        """处理 limit 条历史消息, 并检测是否有验证码或按钮."""
        try:
            answer = None
            captcha = None
            async for m in self.client.get_chat_history(self.chat_name or self.bot_username, limit=limit):
                if MessageType.ANSWER in self.message_type(m):
                    answer = answer or m
                if MessageType.CAPTCHA in self.message_type(m):
                    captcha = captcha or m
                if answer and captcha:
                    break
            else:
                return False
            await self.on_answer(answer)
            await self.on_photo(captcha)
            return True
        except Exception as e:
            self.log.warning("读取历史消息失败, 将不再读取历史消息.")
            show_exception(e)
            return False

    def get_keys(self, message: Message):
        """获得所有按钮信息."""
        reply_markup = message.reply_markup
        if isinstance(reply_markup, InlineKeyboardMarkup):
            return [k.text for r in reply_markup.inline_keyboard for k in r]
        elif isinstance(reply_markup, ReplyKeyboardMarkup):
            return [k.text for r in reply_markup.keyboard for k in r]

    def is_valid_answer(self, message: Message):
        """确认消息是回复按钮消息."""
        if not message.reply_markup:
            return False
        if self.bot_checkin_button_pat:
            for k in self.get_keys(message):
                if not re.search(self.bot_checkin_button_pat, k):
                    return False
        return True

    def message_type(self, message: Message):
        """分析传入消息的类型为验证码或文字."""
        if self.is_valid_answer(message):
            return MessageType.ANSWER | super().message_type(message)
        else:
            return super().message_type(message)

    async def message_handler(self, client: Client, message: Message, type=None):
        """消息处理入口函数."""
        type = type or self.message_type(message)
        if MessageType.ANSWER in type:
            await self.on_answer(message)
        await super().message_handler(client, message, type=type)

    async def on_answer(self, message: Message):
        """当检测到带按钮的用于回答的消息时保存其信息."""
        async with self.mutex:
            if self.message:
                if self.message.date > message.date:
                    return
                else:
                    self.message = message
            else:
                self.message = message
                self.operable.notify()

    async def on_captcha(self, message: Message, captcha: str):
        """
        可修改的回调函数.
        参数:
            message: 包含验证码图片的消息
            captcha: OCR 识别的验证码
        """
        async with self.operable:
            if not self.message:
                await self.operable.wait()
            match = [(k, fuzz.ratio(k, captcha)) for k in self.get_keys(self.message)]
            max_k, max_r = max(match, key=lambda x: x[1])
            if max_r < 75:
                self.log.info(f'未能找到对应 "{captcha}" 的按键, 正在重试.')
                await self.retry()
            else:
                await self.message.click(max_k)
