#!/usr/bin/env python3
# UVC MJPEG камера -> аппаратный декодер bcm2835-codec -> HDMI через DRM/KMS, без GStreamer.
# Только стандартная библиотека. Особенности BMPCC 6K (Gen 1, бета 8.6), см. docs/01:
#  - кадры приходят со сдвигом: хвост с EOI уезжает в следующий буфер -> пересборка SOI..EOI;
#  - иногда в кадре теряются данные, декодер на нём выдаёт ложный LAST -> быстрый сброс.
# Код выхода: 0 — остановлен сигналом, 1 — камера пропала, 2 — нет дисплея/доступа к DRM.
import ctypes
import errno
import fcntl
import mmap
import os
import select
import signal
import sys
import time

CAM = os.environ.get("CAMERA_DEV", "/dev/v4l/by-id/blackmagic-uvc-video-index0")
DEC = os.environ.get("DECODER_DEV", "/dev/video10")
DRI = os.environ.get("DRI_DEV", "/dev/dri/card0")
W = int(os.environ.get("WIDTH", "1920"))
H = int(os.environ.get("HEIGHT", "1080"))
STALL = float(os.environ.get("STALL_SECS", "1.0"))
# Камера добивает каждый буфер до кратного 64 байтам; некратный размер = вырван кусок
# данных. Такой кадр не отдаём декодеру: на экране остаётся предыдущий (40 мс) вместо глитча.
DROP_BAD = os.environ.get("DROP_BAD", "1") == "1"
# Управление: кнопка на GPIO (короткое нажатие — зеркало, долгое — следующая сетка)
# и сигналы для SSH: SIGUSR1 — зеркало, SIGUSR2 — следующая сетка.
MIRROR = os.environ.get("MIRROR", "0") == "1"
GRIDS = ["off", "thirds", "2.39:1", "4:5"]
GRID = os.environ.get("GRID", "off")
GPIO_CHIP = os.environ.get("GPIO_CHIP", "/dev/gpiochip0")
BUTTON_GPIO = os.environ.get("BUTTON_GPIO", "21")   # пин 40, земля — соседний пин 39
LONG_PRESS = 0.8
CAMERA_SILENT = 2.0
SOI, EOI = b"\xff\xd8", b"\xff\xd9"

u8, u16, u32, u64, s32 = ctypes.c_uint8, ctypes.c_uint16, ctypes.c_uint32, ctypes.c_uint64, ctypes.c_int32


def fourcc(s):
    return int.from_bytes(s.encode(), "little")


def log(msg):
    print(f"[hwbridge] {msg}", flush=True)


# ---------- V4L2 ----------

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


class ExportBuf(ctypes.Structure):
    _fields_ = [("type", u32), ("index", u32), ("plane", u32), ("flags", u32),
                ("fd", s32), ("reserved", u32 * 11)]


def vioc(rw, nr, size):
    return (rw << 30) | (size << 16) | (ord("V") << 8) | nr


S_FMT, G_FMT, REQBUFS = vioc(3, 5, 204), vioc(3, 4, 204), vioc(3, 8, 20)
QUERYBUF, QBUF, EXPBUF, DQBUF = vioc(3, 9, 68), vioc(3, 15, 68), vioc(3, 16, 64), vioc(3, 17, 68)
STREAMON, STREAMOFF = vioc(1, 18, 4), vioc(1, 19, 4)
CAP, OUT_MP, CAP_MP, MMAP, FIELD_NONE = 1, 10, 9, 1, 1
FLAG_LAST, FLAG_ERROR = 0x00100000, 0x40

# ---------- DRM/KMS ----------


class CardRes(ctypes.Structure):
    _fields_ = [("fb_id_ptr", u64), ("crtc_id_ptr", u64), ("connector_id_ptr", u64),
                ("encoder_id_ptr", u64), ("count_fbs", u32), ("count_crtcs", u32),
                ("count_connectors", u32), ("count_encoders", u32), ("min_width", u32),
                ("max_width", u32), ("min_height", u32), ("max_height", u32)]


class GetConnector(ctypes.Structure):
    _fields_ = [("encoders_ptr", u64), ("modes_ptr", u64), ("props_ptr", u64),
                ("prop_values_ptr", u64), ("count_modes", u32), ("count_props", u32),
                ("count_encoders", u32), ("encoder_id", u32), ("connector_id", u32),
                ("connector_type", u32), ("connector_type_id", u32), ("connection", u32),
                ("mm_width", u32), ("mm_height", u32), ("subpixel", u32), ("pad", u32)]


class GetEncoder(ctypes.Structure):
    _fields_ = [("encoder_id", u32), ("encoder_type", u32), ("crtc_id", u32),
                ("possible_crtcs", u32), ("possible_clones", u32)]


class ModeInfo(ctypes.Structure):
    _fields_ = [("clock", u32), ("hdisplay", u16), ("hsync_start", u16), ("hsync_end", u16),
                ("htotal", u16), ("hskew", u16), ("vdisplay", u16), ("vsync_start", u16),
                ("vsync_end", u16), ("vtotal", u16), ("vscan", u16), ("vrefresh", u32),
                ("flags", u32), ("type", u32), ("name", ctypes.c_char * 32)]


class Crtc(ctypes.Structure):
    _fields_ = [("set_connectors_ptr", u64), ("count_connectors", u32), ("crtc_id", u32),
                ("fb_id", u32), ("x", u32), ("y", u32), ("gamma_size", u32),
                ("mode_valid", u32), ("mode", ModeInfo)]


class PlaneRes(ctypes.Structure):
    _fields_ = [("plane_id_ptr", u64), ("count_planes", u32)]


class GetPlane(ctypes.Structure):
    _fields_ = [("plane_id", u32), ("crtc_id", u32), ("fb_id", u32), ("possible_crtcs", u32),
                ("gamma_size", u32), ("count_format_types", u32), ("format_type_ptr", u64)]


class FbCmd2(ctypes.Structure):
    _fields_ = [("fb_id", u32), ("width", u32), ("height", u32), ("pixel_format", u32),
                ("flags", u32), ("handles", u32 * 4), ("pitches", u32 * 4),
                ("offsets", u32 * 4), ("modifier", u64 * 4)]


class SetPlane(ctypes.Structure):
    _fields_ = [("plane_id", u32), ("crtc_id", u32), ("fb_id", u32), ("flags", u32),
                ("crtc_x", s32), ("crtc_y", s32), ("crtc_w", u32), ("crtc_h", u32),
                ("src_x", u32), ("src_y", u32), ("src_h", u32), ("src_w", u32)]


class PrimeHandle(ctypes.Structure):
    _fields_ = [("handle", u32), ("flags", u32), ("fd", s32)]


class CreateDumb(ctypes.Structure):
    _fields_ = [("height", u32), ("width", u32), ("bpp", u32), ("flags", u32),
                ("handle", u32), ("pitch", u32), ("size", u64)]


class MapDumb(ctypes.Structure):
    _fields_ = [("handle", u32), ("pad", u32), ("offset", u64)]


class ObjGetProps(ctypes.Structure):
    _fields_ = [("props_ptr", u64), ("prop_values_ptr", u64), ("count_props", u32),
                ("obj_id", u32), ("obj_type", u32)]


class GetProperty(ctypes.Structure):
    _fields_ = [("values_ptr", u64), ("enum_blob_ptr", u64), ("prop_id", u32), ("flags", u32),
                ("name", ctypes.c_char * 32), ("count_values", u32), ("count_enum_blobs", u32)]


class ObjSetProp(ctypes.Structure):
    _fields_ = [("value", u64), ("prop_id", u32), ("obj_id", u32), ("obj_type", u32)]


def dioc(rw, nr, size):
    return (rw << 30) | (size << 16) | (ord("d") << 8) | nr


GETRESOURCES, GETCRTC = dioc(3, 0xA0, 64), dioc(3, 0xA1, 104)
GETENCODER, GETCONNECTOR = dioc(3, 0xA6, 20), dioc(3, 0xA7, 80)
GETPLANERESOURCES, GETPLANE = dioc(3, 0xB5, 16), dioc(3, 0xB6, 32)
SETPLANE_IOC, ADDFB2 = dioc(3, 0xB7, 48), dioc(3, 0xB8, 104)
PRIME_FD_TO_HANDLE, SET_MASTER = dioc(3, 0x2E, 12), dioc(0, 0x1E, 0)
CREATE_DUMB, MAP_DUMB = dioc(3, 0xB2, 32), dioc(3, 0xB3, 16)
GETPROPERTY, OBJ_GETPROPERTIES, OBJ_SETPROPERTY = dioc(3, 0xAA, 64), dioc(3, 0xB9, 32), dioc(3, 0xBA, 24)
OBJECT_PLANE = 0xEEEEEEEE
ROTATE_0, REFLECT_X = 1 << 0, 1 << 4

# ---------- GPIO (chardev uAPI v2): кнопка без внешних резисторов, внутренняя подтяжка ----------


class GpioAttr(ctypes.Structure):
    _fields_ = [("id", u32), ("padding", u32), ("value", u64)]


class GpioCfgAttr(ctypes.Structure):
    _fields_ = [("attr", GpioAttr), ("mask", u64)]


class GpioLineConfig(ctypes.Structure):
    _fields_ = [("flags", u64), ("num_attrs", u32), ("padding", u32 * 5), ("attrs", GpioCfgAttr * 10)]


class GpioLineRequest(ctypes.Structure):
    _fields_ = [("offsets", u32 * 64), ("consumer", ctypes.c_char * 32), ("config", GpioLineConfig),
                ("num_lines", u32), ("event_buffer_size", u32), ("padding", u32 * 5), ("fd", s32)]


GPIO_GET_LINE = (3 << 30) | (592 << 16) | (0xB4 << 8) | 0x07
GPIO_INPUT, GPIO_EDGE_RISING, GPIO_EDGE_FALLING, GPIO_PULL_UP = 1 << 2, 1 << 4, 1 << 5, 1 << 8
GPIO_ATTR_DEBOUNCE = 3

for cls, size in [(Buffer, 68), (Format, 204), (ReqBufs, 20), (ExportBuf, 64), (CardRes, 64),
                  (GetConnector, 80), (GetEncoder, 20), (ModeInfo, 68), (Crtc, 104),
                  (PlaneRes, 16), (GetPlane, 32), (FbCmd2, 104), (SetPlane, 48), (PrimeHandle, 12),
                  (CreateDumb, 32), (MapDumb, 16), (ObjGetProps, 32), (GetProperty, 64),
                  (ObjSetProp, 24), (GpioLineConfig, 272), (GpioLineRequest, 592)]:
    assert ctypes.sizeof(cls) == size, (cls.__name__, ctypes.sizeof(cls))


def ioctl(fd, req, arg):
    fcntl.ioctl(fd, req, arg)
    return arg


class Display:
    def __init__(self):
        self.fd = os.open(DRI, os.O_RDWR | os.O_CLOEXEC)
        try:
            fcntl.ioctl(self.fd, SET_MASTER)
        except OSError:
            pass
        res = ioctl(self.fd, GETRESOURCES, CardRes())
        crtcs = (u32 * res.count_crtcs)()
        conns = (u32 * res.count_connectors)()
        res2 = CardRes(crtc_id_ptr=ctypes.addressof(crtcs), count_crtcs=res.count_crtcs,
                       connector_id_ptr=ctypes.addressof(conns),
                       count_connectors=res.count_connectors)
        ioctl(self.fd, GETRESOURCES, res2)
        self.crtc_id = None
        for cid in conns:
            c = ioctl(self.fd, GETCONNECTOR, GetConnector(connector_id=cid))
            if c.connection == 1 and c.encoder_id:
                enc = ioctl(self.fd, GETENCODER, GetEncoder(encoder_id=c.encoder_id))
                if enc.crtc_id:
                    self.crtc_id = enc.crtc_id
                    break
        if self.crtc_id is None:
            raise RuntimeError("нет подключённого дисплея с активным CRTC")
        crtc_index = list(crtcs).index(self.crtc_id)
        mode = ioctl(self.fd, GETCRTC, Crtc(crtc_id=self.crtc_id)).mode
        self.mw, self.mh = mode.hdisplay, mode.vdisplay

        pr = ioctl(self.fd, GETPLANERESOURCES, PlaneRes())
        pids = (u32 * pr.count_planes)()
        ioctl(self.fd, GETPLANERESOURCES,
              PlaneRes(plane_id_ptr=ctypes.addressof(pids), count_planes=pr.count_planes))
        nv12, argb = fourcc("NV12"), fourcc("AR24")
        free = []
        for pid in pids:
            p = ioctl(self.fd, GETPLANE, GetPlane(plane_id=pid))
            fmts = (u32 * p.count_format_types)()
            p = ioctl(self.fd, GETPLANE, GetPlane(plane_id=pid, count_format_types=p.count_format_types,
                                                  format_type_ptr=ctypes.addressof(fmts)))
            if p.possible_crtcs & (1 << crtc_index) and p.fb_id == 0:
                free.append((pid, set(fmts)))
        video = [pid for pid, f in free if nv12 in f]
        if not video:
            raise RuntimeError("нет свободной overlay-плоскости с NV12")
        self.plane_id = video[0]
        # сетка — на следующей свободной плоскости (выше видео по умолчанию)
        grid = [pid for pid, f in free if argb in f and pid > self.plane_id]
        self.grid_plane = grid[0] if grid else None
        self.grid_fb = None
        self.mirror = False
        scale = min(self.mw / W, self.mh / H)
        self.dw, self.dh = int(W * scale) & ~1, int(H * scale) & ~1
        self.dx, self.dy = (self.mw - self.dw) // 2, (self.mh - self.dh) // 2
        log(f"дисплей {self.mw}x{self.mh}, плоскость видео {self.plane_id}, сетки {self.grid_plane}, "
            f"вывод {self.dw}x{self.dh}+{self.dx}+{self.dy}")

    def plane_prop(self, plane_id, name):
        cnt = ioctl(self.fd, OBJ_GETPROPERTIES, ObjGetProps(obj_id=plane_id, obj_type=OBJECT_PLANE)).count_props
        ids, vals = (u32 * cnt)(), (u64 * cnt)()
        ioctl(self.fd, OBJ_GETPROPERTIES, ObjGetProps(props_ptr=ctypes.addressof(ids),
                                                      prop_values_ptr=ctypes.addressof(vals),
                                                      count_props=cnt, obj_id=plane_id,
                                                      obj_type=OBJECT_PLANE))
        for pid, val in zip(ids, vals):
            if ioctl(self.fd, GETPROPERTY, GetProperty(prop_id=pid)).name.decode() == name:
                return pid, val
        return None, None

    def set_prop(self, plane_id, name, value):
        pid, _ = self.plane_prop(plane_id, name)
        if pid is None:
            raise OSError(errno.ENOENT, f"у плоскости {plane_id} нет свойства {name}")
        ioctl(self.fd, OBJ_SETPROPERTY, ObjSetProp(value=value, prop_id=pid, obj_id=plane_id,
                                                   obj_type=OBJECT_PLANE))

    def set_mirror(self, on):
        self.set_prop(self.plane_id, "rotation", ROTATE_0 | (REFLECT_X if on else 0))
        self.mirror = on

    # --- сетки и рамки: прозрачный ARGB-буфер во весь экран на отдельной плоскости ---

    def _grid_buffer(self):
        d = ioctl(self.fd, CREATE_DUMB, CreateDumb(width=self.mw, height=self.mh, bpp=32))
        off = ioctl(self.fd, MAP_DUMB, MapDumb(handle=d.handle)).offset
        self.grid_map = mmap.mmap(self.fd, d.size, offset=off)
        self.grid_pitch = d.pitch
        fb = FbCmd2(width=self.mw, height=self.mh, pixel_format=fourcc("AR24"))
        fb.handles[0], fb.pitches[0] = d.handle, d.pitch
        self.grid_fb = ioctl(self.fd, ADDFB2, fb).fb_id
        vz, v = self.plane_prop(self.plane_id, "zpos")
        if vz is not None:
            try:
                self.set_prop(self.grid_plane, "zpos", v + 1)
            except OSError:
                pass

    def _fill(self, x0, y0, x1, y1, px):
        row = px * (x1 - x0)
        for y in range(max(y0, 0), min(y1, self.mh)):
            o = y * self.grid_pitch + x0 * 4
            self.grid_map[o:o + len(row)] = row

    def _frame(self, x0, y0, x1, y1, line, t=2):
        self._fill(x0, y0, x1, y0 + t, line)
        self._fill(x0, y1 - t, x1, y1, line)
        self._fill(x0, y0, x0 + t, y1, line)
        self._fill(x1 - t, y0, x1, y1, line)

    def set_grid(self, pattern):
        if self.grid_plane is None:
            raise OSError(errno.ENODEV, "нет свободной плоскости под сетку")
        if pattern == "off":
            ioctl(self.fd, SETPLANE_IOC, SetPlane(plane_id=self.grid_plane, crtc_id=self.crtc_id))
            return
        if self.grid_fb is None:
            self._grid_buffer()
        self.grid_map[:] = bytes(len(self.grid_map))
        line = bytes((255, 255, 255, 170))   # BGRA: белый, полупрозрачный
        mask = bytes((0, 0, 0, 150))         # затемнение вне рамки
        x0, y0, w, h = self.dx, self.dy, self.dw, self.dh
        if pattern == "thirds":
            for k in (1, 2):
                self._fill(x0 + w * k // 3 - 1, y0, x0 + w * k // 3 + 1, y0 + h, line)
                self._fill(x0, y0 + h * k // 3 - 1, x0 + w, y0 + h * k // 3 + 1, line)
            cx, cy = x0 + w // 2, y0 + h // 2
            self._fill(cx - 20, cy - 1, cx + 20, cy + 1, line)
            self._fill(cx - 1, cy - 20, cx + 1, cy + 20, line)
        else:
            a, b = (float(v) for v in pattern.split(":"))
            aspect = a / b
            if aspect > w / h:
                fh = int(w / aspect)
                fx0, fy0, fx1, fy1 = x0, y0 + (h - fh) // 2, x0 + w, y0 + (h + fh) // 2
            else:
                fw = int(h * aspect)
                fx0, fy0, fx1, fy1 = x0 + (w - fw) // 2, y0, x0 + (w + fw) // 2, y0 + h
            self._fill(x0, y0, x0 + w, fy0, mask)
            self._fill(x0, fy1, x0 + w, y0 + h, mask)
            self._fill(x0, fy0, fx0, fy1, mask)
            self._fill(fx1, fy0, x0 + w, fy1, mask)
            self._frame(fx0, fy0, fx1, fy1, line)
        ioctl(self.fd, SETPLANE_IOC, SetPlane(
            plane_id=self.grid_plane, crtc_id=self.crtc_id, fb_id=self.grid_fb,
            crtc_x=0, crtc_y=0, crtc_w=self.mw, crtc_h=self.mh,
            src_x=0, src_y=0, src_w=self.mw << 16, src_h=self.mh << 16))

    def add_fb(self, dmabuf_fd, width, height, pitch):
        h = ioctl(self.fd, PRIME_FD_TO_HANDLE, PrimeHandle(fd=dmabuf_fd)).handle
        fb = FbCmd2(width=width, height=height, pixel_format=fourcc("NV12"))
        fb.handles[0] = fb.handles[1] = h
        fb.pitches[0] = fb.pitches[1] = pitch
        fb.offsets[1] = pitch * height
        return ioctl(self.fd, ADDFB2, fb).fb_id

    def show(self, fb_id):
        ioctl(self.fd, SETPLANE_IOC, SetPlane(
            plane_id=self.plane_id, crtc_id=self.crtc_id, fb_id=fb_id,
            crtc_x=self.dx, crtc_y=self.dy, crtc_w=self.dw, crtc_h=self.dh,
            src_x=0, src_y=0, src_w=W << 16, src_h=H << 16))


class Button:
    """Кнопка между GPIO и GND, подтяжка к питанию — внутренняя (без резисторов).
    Нажатие = спад (1->0), отпускание = фронт."""
    DEBOUNCE = 0.03

    def __init__(self, line):
        chip = os.open(GPIO_CHIP, os.O_RDWR | os.O_CLOEXEC)
        try:
            req = GpioLineRequest(num_lines=1, consumer=b"usbc-hwbridge")
            req.offsets[0] = line
            req.config.flags = GPIO_INPUT | GPIO_EDGE_RISING | GPIO_EDGE_FALLING | GPIO_PULL_UP
            ioctl(chip, GPIO_GET_LINE, req)
        finally:
            os.close(chip)
        self.fd = req.fd
        os.set_blocking(self.fd, False)
        self.pressed_at = None
        self.last_edge = 0.0

    def events(self):
        """Возвращает список нажатий: 'short' или 'long'."""
        out = []
        try:
            data = os.read(self.fd, 48 * 16)
        except BlockingIOError:
            return out
        now = time.monotonic()
        for off in range(0, len(data) - 47, 48):
            ev_id = int.from_bytes(data[off + 8:off + 12], "little")
            if now - self.last_edge < self.DEBOUNCE:
                continue
            self.last_edge = now
            if ev_id == 2:                      # спад: нажали
                self.pressed_at = now
            elif ev_id == 1 and self.pressed_at is not None:   # фронт: отпустили
                out.append("long" if now - self.pressed_at >= LONG_PRESS else "short")
                self.pressed_at = None
        return out


# ---------- мост ----------

def v4l2_dqbuf(fd, b):
    try:
        fcntl.ioctl(fd, DQBUF, b)
        return True
    except OSError as e:
        if e.errno == errno.EAGAIN:
            return False
        raise


class Bridge:
    def __init__(self, display):
        self.disp = display
        # камера
        self.cam = os.open(CAM, os.O_RDWR | os.O_NONBLOCK)
        f = Format(type=CAP)
        f.fmt.pix.width, f.fmt.pix.height = W, H
        f.fmt.pix.pixelformat, f.fmt.pix.field = fourcc("MJPG"), FIELD_NONE
        ioctl(self.cam, S_FMT, f)
        rb = ioctl(self.cam, REQBUFS, ReqBufs(count=4, type=CAP, memory=MMAP))
        self.cam_maps = []
        for i in range(rb.count):
            b = ioctl(self.cam, QUERYBUF, Buffer(type=CAP, index=i, memory=MMAP))
            self.cam_maps.append(mmap.mmap(self.cam, b.length, offset=b.m))
            ioctl(self.cam, QBUF, b)

        # декодер
        self.dec = os.open(DEC, os.O_RDWR | os.O_NONBLOCK)
        f = Format(type=OUT_MP)
        mp = f.fmt.pix_mp
        mp.width, mp.height, mp.pixelformat, mp.field, mp.num_planes = W, H, fourcc("MJPG"), FIELD_NONE, 1
        mp.plane_fmt[0].sizeimage = 2 * 1024 * 1024
        ioctl(self.dec, S_FMT, f)
        f = Format(type=CAP_MP)
        mp = f.fmt.pix_mp
        mp.width, mp.height, mp.pixelformat, mp.field, mp.num_planes = W, H, fourcc("NV12"), FIELD_NONE, 1
        ioctl(self.dec, S_FMT, f)
        ioctl(self.dec, G_FMT, f)
        bh, pitch = f.fmt.pix_mp.height, f.fmt.pix_mp.plane_fmt[0].bytesperline

        self.out_maps = []
        n = ioctl(self.dec, REQBUFS, ReqBufs(count=4, type=OUT_MP, memory=MMAP)).count
        for i in range(n):
            p = Plane()
            ioctl(self.dec, QUERYBUF, self.mp_buf(OUT_MP, i, p))
            self.out_maps.append(mmap.mmap(self.dec, p.length, offset=p.mem_offset))
        self.ncap = ioctl(self.dec, REQBUFS, ReqBufs(count=6, type=CAP_MP, memory=MMAP)).count
        self.fbs = []
        for i in range(self.ncap):
            e = ioctl(self.dec, EXPBUF, ExportBuf(type=CAP_MP, index=i, flags=os.O_CLOEXEC))
            self.fbs.append(self.disp.add_fb(e.fd, W, bh, pitch))
            os.close(e.fd)
        log(f"декодер: выход {W}x{bh} NV12, pitch {pitch}, {self.ncap} буферов")

        self.free_out = list(range(len(self.out_maps)))
        self.cur = None
        self.pos = 0
        self.cap_on = False
        self.displayed = None
        ioctl(self.dec, STREAMON, ctypes.c_int(OUT_MP))
        ioctl(self.cam, STREAMON, ctypes.c_int(CAP))
        self.t_in = self.t_out = time.monotonic()
        self.frames = self.shown = self.dropped = self.resets = 0
        self.bad_frames = self.dec_errors = 0
        self.cur_bad = False

        self.button = None
        if BUTTON_GPIO:
            try:
                self.button = Button(int(BUTTON_GPIO))
                log(f"кнопка: GPIO{BUTTON_GPIO} (короткое — зеркало, долгое — сетка)")
            except OSError as e:
                log(f"кнопка недоступна: {e}")
        self.grid_idx = GRIDS.index(GRID) if GRID in GRIDS else 0
        if MIRROR:
            self.toggle_mirror()
        if self.grid_idx:
            self.apply_grid()

    def toggle_mirror(self):
        try:
            self.disp.set_mirror(not self.disp.mirror)
            log(f"зеркало: {'вкл' if self.disp.mirror else 'выкл'}")
        except OSError as e:
            log(f"зеркало не поддерживается: {e}")

    def next_grid(self):
        self.grid_idx = (self.grid_idx + 1) % len(GRIDS)
        self.apply_grid()

    def apply_grid(self):
        try:
            self.disp.set_grid(GRIDS[self.grid_idx])
            log(f"сетка: {GRIDS[self.grid_idx]}")
        except OSError as e:
            log(f"сетка недоступна: {e}")

    @staticmethod
    def mp_buf(btype, index, planes):
        return Buffer(type=btype, index=index, memory=MMAP, length=1, m=ctypes.addressof(planes))

    # --- пересборка кадров прямо в буферы декодера (одна копия на кадр) ---

    def begin(self, bad):
        if not self.free_out:
            self.dropped += 1
            return False
        self.cur, self.pos, self.cur_bad = self.free_out.pop(), 0, bad
        return True

    def append(self, mv, a, b):
        n = b - a
        out = self.out_maps[self.cur]
        if self.pos + n > len(out):
            self.drop()
            return False
        out[self.pos:self.pos + n] = mv[a:b]
        self.pos += n
        return True

    def drop(self):
        if self.cur is not None:
            self.free_out.append(self.cur)
            self.cur = None

    def submit(self):
        if self.cur_bad and DROP_BAD:
            self.bad_frames += 1
            self.drop()
            return
        p = Plane(bytesused=self.pos)
        b = self.mp_buf(OUT_MP, self.cur, p)
        b.bytesused = self.pos
        self.frames += 1
        b.timestamp.sec = self.frames
        ioctl(self.dec, QBUF, b)
        self.cur = None
        if not self.cap_on:
            self.start_capture()

    def on_cam_data(self, mm, n):
        bad = n % 64 != 0
        mv = memoryview(mm)
        s = mm.find(SOI, 0, min(n, 4096))
        tail_end = s if s >= 0 else n
        if self.cur is not None:
            e = mm.find(EOI, 0, tail_end)
            if e >= 0:
                if self.append(mv, 0, e + 2):
                    self.submit()
            elif n and mm[0] == 0xD9 and self.pos and self.out_maps[self.cur][self.pos - 1] == 0xFF:
                if self.append(mv, 0, 1):
                    self.submit()
            elif s >= 0:
                self.drop()
            else:
                self.append(mv, 0, n)
                return
        if s < 0 or not self.begin(bad):
            return
        e = mm.rfind(EOI, s + 2, n)
        if e >= 0 and n - (e + 2) < 128:
            if self.append(mv, s, e + 2):
                self.submit()
        else:
            self.append(mv, s, n)

    # --- декодер ---

    def queue_cap(self, i):
        p = Plane()
        ioctl(self.dec, QBUF, self.mp_buf(CAP_MP, i, p))

    def start_capture(self):
        ioctl(self.dec, STREAMON, ctypes.c_int(CAP_MP))
        for i in range(self.ncap):
            if i != self.displayed:
                self.queue_cap(i)
        self.cap_on = True

    def reset(self, why):
        self.resets += 1
        log(f"сброс декодера #{self.resets}: {why}")
        ioctl(self.dec, STREAMOFF, ctypes.c_int(CAP_MP))
        ioctl(self.dec, STREAMOFF, ctypes.c_int(OUT_MP))
        self.free_out = list(range(len(self.out_maps)))
        self.cur = None
        self.cap_on = False
        ioctl(self.dec, STREAMON, ctypes.c_int(OUT_MP))
        self.t_out = time.monotonic()

    def on_decoded(self):
        p = Plane()
        b = self.mp_buf(CAP_MP, 0, p)
        while self.cap_on:
            try:
                if not v4l2_dqbuf(self.dec, b):
                    return
            except OSError as e:
                if e.errno == errno.EPIPE:
                    self.reset("EPIPE")
                    return
                raise
            if b.flags & FLAG_LAST or p.bytesused == 0:
                self.reset("ложный LAST")
                return
            if b.flags & FLAG_ERROR:
                # декодер сам пометил кадр битым — не показываем, остаётся предыдущий
                self.dec_errors += 1
                self.queue_cap(b.index)
                continue
            self.disp.show(self.fbs[b.index])
            self.shown += 1
            self.t_out = time.monotonic()
            if self.displayed is not None:
                self.queue_cap(self.displayed)
            self.displayed = b.index

    def on_out_done(self):
        p = Plane()
        b = self.mp_buf(OUT_MP, 0, p)
        while v4l2_dqbuf(self.dec, b):
            if b.index != self.cur and b.index not in self.free_out:
                self.free_out.append(b.index)

    def on_camera(self):
        b = Buffer(type=CAP, memory=MMAP)
        while v4l2_dqbuf(self.cam, b):
            self.t_in = time.monotonic()
            if b.flags & FLAG_ERROR:
                self.drop()
            elif b.bytesused:
                self.on_cam_data(self.cam_maps[b.index], b.bytesused)
            ioctl(self.cam, QBUF, b)

    def run(self):
        running = [True]
        pending = {"mirror": False, "grid": False}
        signal.signal(signal.SIGTERM, lambda *_: running.__setitem__(0, False))
        signal.signal(signal.SIGINT, lambda *_: running.__setitem__(0, False))
        signal.signal(signal.SIGUSR1, lambda *_: pending.__setitem__("mirror", True))
        signal.signal(signal.SIGUSR2, lambda *_: pending.__setitem__("grid", True))
        watch = [self.cam, self.dec] + ([self.button.fd] if self.button else [])
        t_rep, shown_rep = time.monotonic(), 0
        while running[0]:
            try:
                r, w, _ = select.select(watch, [self.dec], [], 0.5)
            except InterruptedError:
                continue
            if self.button and self.button.fd in r:
                for press in self.button.events():
                    if press == "short":
                        self.toggle_mirror()
                    else:
                        self.next_grid()
            if pending["mirror"]:
                pending["mirror"] = False
                self.toggle_mirror()
            if pending["grid"]:
                pending["grid"] = False
                self.next_grid()
            if self.cam in r:
                self.on_camera()
            if self.dec in w:
                self.on_out_done()
            if self.dec in r and self.cap_on:
                self.on_decoded()
            now = time.monotonic()
            if now - self.t_in > CAMERA_SILENT:
                log("камера молчит, выхожу")
                return 1
            if self.cap_on and now - self.t_out > STALL and now - self.t_in < 0.5:
                self.reset(f"нет кадров {now - self.t_out:.1f} с")
            if now - t_rep >= 60:
                log(f"за минуту на экран {(self.shown - shown_rep) / (now - t_rep):.1f} fps, "
                    f"сбросов всего {self.resets}, выброшено {self.dropped}, "
                    f"битых по размеру {self.bad_frames}, с флагом ошибки {self.dec_errors}")
                t_rep, shown_rep = now, self.shown
        return 0


def main():
    try:
        disp = Display()
    except (OSError, RuntimeError, ValueError) as e:
        log(f"дисплей: {e}")
        return 2
    try:
        bridge = Bridge(disp)
    except OSError as e:
        log(f"камера/декодер: {e}")
        return 1
    return bridge.run()


if __name__ == "__main__":
    sys.exit(main())
