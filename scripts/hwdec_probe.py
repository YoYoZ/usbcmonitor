#!/usr/bin/env python3
# Эксперимент: MJPEG с камеры -> bcm2835-codec напрямую через V4L2 (без GStreamer).
# Проверяет, можно ли продолжить декодирование после ложного V4L2 LAST от драйвера.
# На экран ничего не выводит, только статистика. Запуск: python3 hwdec_probe.py [секунды]
import collections
import ctypes
import errno
import fcntl
import mmap
import os
import select
import sys
import time

CAM = os.environ.get("CAMERA_DEV", "/dev/v4l/by-id/blackmagic-uvc-video-index0")
DEC = os.environ.get("DECODER_DEV", "/dev/video10")
W, H = 1920, 1080
DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 300
CAPFMT = os.environ.get("CAPFMT", "YU12")
RECOVER = os.environ.get("RECOVER", "full")
# SRCFILE: вместо кадров камеры подавать этот JPEG (камера задаёт только темп 25 fps).
SRCFILE = os.environ.get("SRCFILE")
SRCDATA = open(SRCFILE, "rb").read() if SRCFILE else b""
REFRAME = os.environ.get("REFRAME", "1") == "1"
DUMP = os.environ.get("DUMP")  # каталог: при ложном LAST сохранить последние кадры

u8, u16, u32 = ctypes.c_uint8, ctypes.c_uint16, ctypes.c_uint32


def fourcc(s):
    return ord(s[0]) | ord(s[1]) << 8 | ord(s[2]) << 16 | ord(s[3]) << 24


class Timeval(ctypes.Structure):
    _fields_ = [("sec", ctypes.c_long), ("usec", ctypes.c_long)]


class Timecode(ctypes.Structure):
    _fields_ = [("type", u32), ("flags", u32), ("f", u8 * 4), ("ub", u8 * 4)]


class Plane(ctypes.Structure):
    _fields_ = [("bytesused", u32), ("length", u32), ("mem_offset", u32),
                ("data_offset", u32), ("reserved", u32 * 11)]


class Buffer(ctypes.Structure):
    _fields_ = [("index", u32), ("type", u32), ("bytesused", u32), ("flags", u32),
                ("field", u32), ("timestamp", Timeval), ("timecode", Timecode),
                ("sequence", u32), ("memory", u32), ("m", u32), ("length", u32),
                ("reserved2", u32), ("request_fd", u32)]


class PixFmt(ctypes.Structure):
    _fields_ = [("width", u32), ("height", u32), ("pixelformat", u32), ("field", u32),
                ("bytesperline", u32), ("sizeimage", u32), ("colorspace", u32),
                ("priv", u32), ("flags", u32), ("ycbcr_enc", u32),
                ("quantization", u32), ("xfer_func", u32)]


class PlanePixFmt(ctypes.Structure):
    _fields_ = [("sizeimage", u32), ("bytesperline", u32), ("reserved", u16 * 6)]


class PixFmtMp(ctypes.Structure):
    _fields_ = [("width", u32), ("height", u32), ("pixelformat", u32), ("field", u32),
                ("colorspace", u32), ("plane_fmt", PlanePixFmt * 8), ("num_planes", u8),
                ("flags", u8), ("ycbcr_enc", u8), ("quantization", u8),
                ("xfer_func", u8), ("reserved", u8 * 7)]


class FmtUnion(ctypes.Union):
    _fields_ = [("pix", PixFmt), ("pix_mp", PixFmtMp), ("raw", u8 * 200)]


class Format(ctypes.Structure):
    _fields_ = [("type", u32), ("fmt", FmtUnion)]


class ReqBufs(ctypes.Structure):
    _fields_ = [("count", u32), ("type", u32), ("memory", u32), ("capabilities", u32),
                ("flags", u8), ("reserved", u8 * 3)]


class DecCmd(ctypes.Structure):
    _fields_ = [("cmd", u32), ("flags", u32), ("raw", u32 * 16)]


assert ctypes.sizeof(Buffer) == 68 and ctypes.sizeof(Format) == 204
assert ctypes.sizeof(ReqBufs) == 20 and ctypes.sizeof(DecCmd) == 72


def ioc(rw, nr, size):
    return (rw << 30) | (size << 16) | (ord("V") << 8) | nr


RW, WR = 3, 1
S_FMT, G_FMT = ioc(RW, 5, 204), ioc(RW, 4, 204)
REQBUFS, QUERYBUF = ioc(RW, 8, 20), ioc(RW, 9, 68)
QBUF, DQBUF = ioc(RW, 15, 68), ioc(RW, 17, 68)
STREAMON, STREAMOFF = ioc(WR, 18, 4), ioc(WR, 19, 4)
DECODER_CMD = ioc(RW, 96, 72)

CAP, OUT_MP, CAP_MP = 1, 10, 9
MMAP = 1
FIELD_NONE = 1
FLAG_LAST, FLAG_ERROR = 0x00100000, 0x40
DEC_CMD_START = 0


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def stream(fd, btype, on=True):
    fcntl.ioctl(fd, STREAMON if on else STREAMOFF, ctypes.c_int(btype))


def reqbufs(fd, btype, count):
    rb = ReqBufs(count=count, type=btype, memory=MMAP)
    fcntl.ioctl(fd, REQBUFS, rb)
    return rb.count


def mp_buf(btype, index, planes):
    return Buffer(type=btype, index=index, memory=MMAP, length=1,
                  m=ctypes.addressof(planes))


def dqbuf(fd, b):
    try:
        fcntl.ioctl(fd, DQBUF, b)
        return True
    except OSError as e:
        if e.errno == errno.EAGAIN:
            return False
        raise


class Probe:
    def __init__(self):
        # камера (single-plane)
        self.cam = os.open(CAM, os.O_RDWR | os.O_NONBLOCK)
        f = Format(type=CAP)
        f.fmt.pix.width, f.fmt.pix.height = W, H
        f.fmt.pix.pixelformat, f.fmt.pix.field = fourcc("MJPG"), FIELD_NONE
        fcntl.ioctl(self.cam, S_FMT, f)
        self.cam_maps = []
        for i in range(reqbufs(self.cam, CAP, 4)):
            b = Buffer(type=CAP, index=i, memory=MMAP)
            fcntl.ioctl(self.cam, QUERYBUF, b)
            self.cam_maps.append(mmap.mmap(self.cam, b.length, offset=b.m))
            fcntl.ioctl(self.cam, QBUF, b)

        # декодер (multi-plane M2M)
        self.dec = os.open(DEC, os.O_RDWR | os.O_NONBLOCK)
        f = Format(type=OUT_MP)
        mp = f.fmt.pix_mp
        mp.width, mp.height, mp.pixelformat, mp.field = W, H, fourcc("MJPG"), FIELD_NONE
        mp.num_planes = 1
        mp.plane_fmt[0].sizeimage = 2 * 1024 * 1024
        fcntl.ioctl(self.dec, S_FMT, f)
        f = Format(type=CAP_MP)
        mp = f.fmt.pix_mp
        mp.width, mp.height, mp.pixelformat, mp.field = W, H, fourcc(CAPFMT), FIELD_NONE
        mp.num_planes = 1
        fcntl.ioctl(self.dec, S_FMT, f)
        fcntl.ioctl(self.dec, G_FMT, f)
        log(f"декодер: выход {f.fmt.pix_mp.width}x{f.fmt.pix_mp.height}, "
            f"sizeimage {f.fmt.pix_mp.plane_fmt[0].sizeimage}")

        self.out_maps = []
        for i in range(reqbufs(self.dec, OUT_MP, 4)):
            p = Plane()
            b = mp_buf(OUT_MP, i, p)
            fcntl.ioctl(self.dec, QUERYBUF, b)
            self.out_maps.append(mmap.mmap(self.dec, p.length, offset=p.mem_offset))
        self.free_out = list(range(len(self.out_maps)))
        self.ncap = reqbufs(self.dec, CAP_MP, 6)
        # Как в GStreamer: вход включаем сразу, выход — только после первого JPEG на входе.
        stream(self.dec, OUT_MP)
        self.cap_on = False
        stream(self.cam, CAP)

        self.acc = bytearray()
        self.hist_fed = collections.deque(maxlen=8)
        self.hist_raw = collections.deque(maxlen=6)
        self.cam_frames = self.dropped = self.fed = self.decoded = 0
        self.lasts = self.recoveries = 0
        self.last_at = None
        self.frame_no = 0

    def start_capture(self):
        stream(self.dec, CAP_MP)
        for i in range(self.ncap):
            self.queue_cap(i)
        self.cap_on = True

    def queue_cap(self, i):
        p = Plane()
        fcntl.ioctl(self.dec, QBUF, mp_buf(CAP_MP, i, p))

    def frames(self, data):
        # Камера отдаёт кадр со сдвигом: последние ~64 байта (с EOI) приходят в начале
        # следующего буфера. Пересобираем поток в кадры SOI..EOI через границы буферов.
        if not REFRAME:
            yield data
            return
        acc = self.acc
        acc += data
        while True:
            s = acc.find(b"\xff\xd8")
            if s < 0:
                acc.clear()
                return
            e = acc.find(b"\xff\xd9", s + 2)
            if e < 0:
                del acc[:s]
                return
            nxt = acc.find(b"\xff\xd8", s + 2, e)
            if nxt >= 0:
                del acc[:nxt]
                continue
            frame = bytes(acc[s:e + 2])
            del acc[:e + 2]
            yield frame

    def feed(self, data):
        self.hist_fed.append(bytes(data))
        n = len(data)
        if not self.free_out:
            self.dropped += 1
            return
        i = self.free_out.pop()
        self.out_maps[i][:n] = data
        p = Plane(bytesused=n)
        ob = mp_buf(OUT_MP, i, p)
        ob.bytesused = n
        self.frame_no += 1
        ob.timestamp.sec = self.frame_no
        fcntl.ioctl(self.dec, QBUF, ob)
        self.fed += 1
        if not self.cap_on:
            self.start_capture()

    def on_camera(self):
        b = Buffer(type=CAP, memory=MMAP)
        while dqbuf(self.cam, b):
            self.cam_frames += 1
            if b.bytesused and not b.flags & FLAG_ERROR:
                data = SRCDATA if SRCFILE else self.cam_maps[b.index][:b.bytesused]
                self.hist_raw.append(data)
                for frame in self.frames(data):
                    self.feed(frame)
            fcntl.ioctl(self.cam, QBUF, b)

    def on_decoder_out(self):
        p = Plane()
        b = mp_buf(OUT_MP, 0, p)
        while dqbuf(self.dec, b):
            self.free_out.append(b.index)

    def on_decoder_cap(self):
        p = Plane()
        b = mp_buf(CAP_MP, 0, p)
        while True:
            try:
                if not dqbuf(self.dec, b):
                    return
            except OSError as e:
                if e.errno == errno.EPIPE:
                    log("DQBUF -> EPIPE (после LAST)")
                    self.recover()
                    return
                raise
            if b.flags & FLAG_LAST or p.bytesused == 0:
                self.lasts += 1
                self.last_at = time.monotonic()
                if DUMP and self.lasts <= 5:
                    d = f"{DUMP}/last{self.lasts}"
                    os.makedirs(d, exist_ok=True)
                    for k, fr in enumerate(self.hist_fed):
                        open(f"{d}/fed_{k}.jpg", "wb").write(fr)
                    for k, raw in enumerate(self.hist_raw):
                        open(f"{d}/raw_{k}.bin", "wb").write(raw)
                    log(f"сохранил {len(self.hist_fed)} поданных кадров и {len(self.hist_raw)} сырых буферов в {d}")
                log(f"ложный LAST #{self.lasts} (bytesused={p.bytesused}, "
                    f"flags=0x{b.flags:x}) после {self.decoded} кадров")
                self.queue_cap(b.index)
                self.recover()
                return
            self.decoded += 1
            if self.last_at is not None:
                log(f"кадры пошли снова через {time.monotonic() - self.last_at:.2f} с")
                self.last_at = None
            self.queue_cap(b.index)

    def recover(self):
        self.recoveries += 1
        if RECOVER == "start":
            try:
                fcntl.ioctl(self.dec, DECODER_CMD, DecCmd(cmd=DEC_CMD_START))
                log("DECODER_CMD START — ok")
                return
            except OSError as e:
                log(f"DECODER_CMD START не принят ({e.strerror})")
        if RECOVER == "restream":
            log("перезапуск очереди выхода (STREAMOFF/STREAMON)")
            stream(self.dec, CAP_MP, on=False)
            stream(self.dec, CAP_MP, on=True)
            for i in range(self.ncap):
                self.queue_cap(i)
            return
        log("полный перезапуск декодера: обе очереди")
        stream(self.dec, CAP_MP, on=False)
        stream(self.dec, OUT_MP, on=False)
        self.free_out = list(range(len(self.out_maps)))
        stream(self.dec, OUT_MP)
        self.cap_on = False

    def run(self):
        start = t_rep = time.monotonic()
        dec_rep = 0
        while time.monotonic() - start < DURATION:
            r, w, _ = select.select([self.cam, self.dec], [self.dec], [], 0.5)
            if self.cam in r:
                self.on_camera()
            if self.dec in w:
                self.on_decoder_out()
            if self.dec in r and self.cap_on:
                self.on_decoder_cap()
            now = time.monotonic()
            if now - t_rep >= 10:
                log(f"камера {self.cam_frames}, в декодер {self.fed}, выброшено {self.dropped}, "
                    f"декодировано {self.decoded} ({(self.decoded - dec_rep) / (now - t_rep):.1f} fps), "
                    f"LAST {self.lasts}")
                t_rep, dec_rep = now, self.decoded
        log(f"итог: декодировано {self.decoded}, LAST {self.lasts}, восстановлений {self.recoveries}")


if __name__ == "__main__":
    try:
        Probe().run()
    except KeyboardInterrupt:
        pass
