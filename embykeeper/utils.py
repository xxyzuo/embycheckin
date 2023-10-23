import asyncio
from collections import namedtuple
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from functools import wraps
from pathlib import Path
import random
import sys
import site
import traceback
from typing import Any, Coroutine, Iterable, Union

import psutil
import click
from loguru import logger
from typer import Typer
from typer.core import TyperCommand

from . import var, __url__, __name__

Flagged = namedtuple("Flagged", ("noflag", "flag"))


def get_path_frame(e, path):
    try:
        tb = traceback.extract_tb(e.__traceback__)
        for frame in reversed(tb):
            if Path(path) in Path(frame.filename).parents:
                return frame
        else:
            return None
    except AttributeError:
        return None


def get_last_frame(e):
    try:
        tb = traceback.extract_tb(e.__traceback__)
        for frame in reversed(tb):
            return frame
    except AttributeError:
        return None


def get_cls_fullpath(c):
    module = c.__module__
    if module == "builtins":
        return c.__qualname__
    return module + "." + c.__qualname__


def format_exception(e, regular=True):
    if regular:
        prompt = "\n请在 Github 或交流群反馈下方错误详情以帮助开发者修复该问题:\n"
    else:
        prompt = ""
    proj_path = Path(__file__).parent.absolute()
    proj_frame = get_path_frame(e, proj_path)
    if proj_frame:
        proj_frame_path = Path(proj_frame.filename).relative_to(proj_path)
        prompt += f"\n  P {proj_frame_path}, L {proj_frame.lineno}, F {proj_frame.name}:"
        prompt += f"\n    {proj_frame.line.strip()}"
    last_frame = get_last_frame(e)
    if last_frame:
        last_frame_path = last_frame.filename
        for p in site.getsitepackages():
            if Path(p) in Path(last_frame.filename).parents:
                last_frame_path = "<SP>/" + str(Path(last_frame.filename).relative_to(p))
                break
        prompt += f"\n  S {last_frame_path}, L {last_frame.lineno}, F {last_frame.name}:"
        prompt += f"\n    {last_frame.line.strip()}"
    prompt += f"\n    E {get_cls_fullpath(type(e))}: {e}\n"
    return prompt


def show_exception(e, regular=True):
    if (regular and 1 < var.debug < 2) or (not regular and var.debug < 2):
        var.console.rule()
        print(format_exception(e, regular=regular), flush=True, file=sys.stderr)
        var.console.rule()
    else:
        logger.opt(exception=e).debug("错误详情:")


class AsyncTyper(Typer):
    """Typer 的异步版本, 所有命令函数都将以异步形式调用."""

    def async_command(self, *args, **kwargs):
        def decorator(async_func):
            @wraps(async_func)
            def sync_func(*_args, **_kwargs):
                try:
                    asyncio.run(async_func(*_args, **_kwargs))
                except KeyboardInterrupt:
                    print("\r", end="", flush=True)
                    logger.info(f"所有客户端已停止, 欢迎您再次使用 {__name__.capitalize()}.")
                except Exception as e:
                    print("\r", end="", flush=True)
                    logger.critical(f"发生关键错误, {__name__.capitalize()} 将退出.")
                    show_exception(e, regular=False)
                    sys.exit(1)
                else:
                    logger.info(f"所有任务已完成, 欢迎您再次使用 {__name__.capitalize()}.")

            self.command(*args, **kwargs)(sync_func)
            return async_func

        return decorator


class FlagValueCommand(TyperCommand):
    """允许在命令行参数中使用"="."""

    def parse_args(self, ctx, args):
        long = {}
        short = {}
        defined = set()
        for o in self.params:
            if isinstance(o, click.Option):
                if isinstance(o.default, Flagged):
                    for pre in o.opts:
                        if pre.startswith("--"):
                            long[pre] = o
                        elif pre.startswith("-"):
                            short[pre] = o

        for i, a in enumerate(args):
            a = a.split("=")
            if a[0] in long:
                defined.add(long[a[0]])
                if len(a) == 1:
                    args[i] = f"{a[0]}={long[a[0]].default.flag}"
            elif a[0] in short:
                defined.add(short[a[0]])
                if len(args) == i + 1 or args[i + 1].startswith("-"):
                    args.insert(i + 1, str(short[a[0]].default.flag))

        for u in set(long.values()) - defined:
            for p, o in long.items():
                if o == u:
                    break
            args.append(f"{p}={u.default.noflag}")

        return super().parse_args(ctx, args)


class AsyncTaskPool:
    """一个用于批量等待异步任务的管理器, 支持在等待时添加任务."""

    def __init__(self):
        self.waiter = asyncio.Condition()
        self.tasks = []

    def add(self, coro: Coroutine):
        async def wrapper():
            task = asyncio.ensure_future(coro)
            await asyncio.wait([task])
            async with self.waiter:
                self.waiter.notify()
                return await task

        t = asyncio.create_task(wrapper())
        t.set_name(coro.__name__)
        self.tasks.append(t)

    async def as_completed(self):
        while self.tasks:
            async with self.waiter:
                await self.waiter.wait()
                for t in self.tasks:
                    if t.done():
                        yield t
                        self.tasks.remove(t)


class AsyncCountPool(dict):
    """
    一个异步安全的 ID 分配器.
    参数:
        base: ID 起始数
    """

    def __init__(self, *args, base=1000, **kw):
        super().__init__(*args, **kw)
        self.lock = asyncio.Lock()
        self.next = base + 1

    async def append(self, value):
        """输入一个值, 该值将被存储并分配一个 ID."""
        async with self.lock:
            key = self.next
            self[key] = value
            self.next += 1
            return key


def to_iterable(var: Union[Iterable, Any]):
    """
    将任何变量变为可迭代变量.
    说明:
        None 将变为空数组.
        非可迭代变量将变为仅有该元素的长度为 1 的数组.
        可迭代变量将保持不变.
    """
    if var is None:
        return ()
    if isinstance(var, str) or not isinstance(var, Iterable):
        return (var,)
    else:
        return var


def remove_prefix(text: str, prefix: str):
    """从字符串头部去除前缀."""
    return text[text.startswith(prefix) and len(prefix) :]


def truncate_str(text: str, length: int):
    """将字符串截断到特定长度, 并增加"..."后缀."""
    return f"{text[:length + 3]}..." if len(text) > length else text


def time_in_range(start, end, x):
    """判定时间在特定范围内."""
    if start <= end:
        return start <= x <= end
    else:
        return start <= x or x <= end


def batch(iterable, n=1):
    """将数组分成 N 份."""
    l = len(iterable)
    for ndx in range(0, l, n):
        yield iterable[ndx : min(ndx + n, l)]


def flatten(l):
    """将二维数组变为一维数组."""
    return [item for sublist in l for item in sublist]


def async_partial(f, *args1, **kw1):
    """Partial 函数的异步形式."""

    async def func(*args2, **kw2):
        return await f(*args1, *args2, **kw1, **kw2)

    return func


async def idle():
    """异步无限等待函数."""
    await asyncio.Event().wait()


def random_time(start_time: time = None, end_time: time = None):
    """在特定的开始和结束时间之间生成时间, 如果开始时间晚于结束时间, 视为过夜."""
    start_datetime = datetime.combine(date.today(), start_time or time(0, 0))
    end_datetime = datetime.combine(date.today(), end_time or time(23, 59, 59))
    if end_datetime < start_datetime:
        end_datetime += timedelta(days=1)
    time_diff_seconds = (end_datetime - start_datetime).seconds
    random_seconds = random.randint(0, time_diff_seconds)
    random_time = (start_datetime + timedelta(seconds=random_seconds)).time()
    return random_time


def next_random_datetime(start_time: time = None, end_time: time = None, interval_days=1):
    """在特定的开始和结束时间之间生成时间, 并设定最小间隔天数."""
    min_datetime = datetime.now() + timedelta(days=interval_days)
    target_time = random_time(start_time, end_time)
    offset_date = 0
    while True:
        offset_date += 1
        t = datetime.combine(datetime.now() + timedelta(days=offset_date), target_time)
        if t >= min_datetime:
            break
    return t


def humanbytes(B: float):
    """将字节数转换为人类可读形式."""
    """Return the given bytes as a human friendly KB, MB, GB, or TB string."""
    B = float(B)
    KB = float(1024)
    MB = float(KB**2)  # 1,048,576
    GB = float(KB**3)  # 1,073,741,824
    TB = float(KB**4)  # 1,099,511,627,776

    if B < KB:
        return "{0} {1}".format(B, "Bytes" if 0 == B > 1 else "Byte")
    elif KB <= B < MB:
        return "{0:.2f} KB".format(B / KB)
    elif MB <= B < GB:
        return "{0:.2f} MB".format(B / MB)
    elif GB <= B < TB:
        return "{0:.2f} GB".format(B / GB)
    elif TB <= B:
        return "{0:.2f} TB".format(B / TB)


def get_file_users(path):
    for proc in psutil.process_iter():
        try:
            files = proc.get_open_files()
            if files:
                for _file in files:
                    if _file.path == path:
                        return proc
        except psutil.NoSuchProcess:
            pass
    else:
        return None


@asynccontextmanager
async def no_waiting(lock: asyncio.Lock):
    try:
        await asyncio.wait_for(lock.acquire(), 0)
    except asyncio.TimeoutError:
        acquired = False
    else:
        acquired = True
    try:
        yield
    finally:
        if acquired:
            lock.release()
