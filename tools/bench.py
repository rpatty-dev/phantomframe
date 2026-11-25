"""
Interpolation quality benchmark on a recorded clip (press R in the app to record one).

For each triplet of consecutive unique frames (A, B, C) it synthesizes the midpoint of A->C with the
app's real shader + flow pipeline and scores it against the real B (PSNR, higher is better).
Baselines: 'repeat' (show A) and 'blend' (crossfade A/C). The app's method must beat 'blend'.

    python tools/bench.py recordings/clip_XXXX.npy
"""
import os, sys, time
import numpy as np, cv2
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import gta6_framegen as g
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QSurfaceFormat, QOffscreenSurface, QOpenGLContext
import OpenGL.GL as gl, OpenGL.GL.shaders as sh

FW, FH = g.CaptureFlowEngine.FLOW_W, g.CaptureFlowEngine.FLOW_H


def main():
    clip = np.load(sys.argv[1], mmap_mode="r")
    app = QApplication(sys.argv)
    fmt = QSurfaceFormat(); fmt.setVersion(3, 3); fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    ctx = QOpenGLContext(); ctx.setFormat(fmt); ctx.create()
    surf = QOffscreenSurface(); surf.setFormat(fmt); surf.create(); ctx.makeCurrent(surf)
    vao = gl.glGenVertexArrays(1); gl.glBindVertexArray(vao)
    prog = sh.compileProgram(sh.compileShader(g.VERTEX_SHADER, gl.GL_VERTEX_SHADER),
                             sh.compileShader(g.FRAGMENT_SHADER, gl.GL_FRAGMENT_SHADER), validate=False)

    def tex(w, h, ifmt, f, t):
        x = gl.glGenTextures(1); gl.glBindTexture(gl.GL_TEXTURE_2D, x)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, ifmt, w, h, 0, f, t, None)
        for p, v in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR), (gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR),
                     (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE), (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)):
            gl.glTexParameteri(gl.GL_TEXTURE_2D, p, v)
        return x

    tA, tC = (tex(1920, 1080, gl.GL_RGB8, gl.GL_BGR, gl.GL_UNSIGNED_BYTE) for _ in range(2))
    flows = [tex(FW, FH, gl.GL_RG32F, gl.GL_RG, gl.GL_FLOAT) for _ in range(2)]
    tOut = tex(1920, 1080, gl.GL_RGBA8, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
    fbo = gl.glGenFramebuffers(1)
    dis = cv2.DISOpticalFlow_create(int(os.environ.get("PRESET", "0"))); dis.setUseSpatialPropagation(True)  # 0 ultrafast, 1 fast, 2 medium

    def upload(t, a):
        gl.glBindTexture(gl.GL_TEXTURE_2D, t)
        gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, 1920, 1080, gl.GL_BGR, gl.GL_UNSIGNED_BYTE, np.ascontiguousarray(a))

    def gray(a):  # same preprocessing as CaptureFlowEngine._flow_loop
        return cv2.GaussianBlur(cv2.cvtColor(cv2.resize(a, (FW, FH), interpolation=cv2.INTER_LINEAR), cv2.COLOR_BGR2GRAY), (3, 3), 0)

    def render():
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0, gl.GL_TEXTURE_2D, tOut, 0)
        gl.glViewport(0, 0, 1920, 1080); gl.glUseProgram(prog)
        L = lambda n: gl.glGetUniformLocation(prog, n)
        for unit, (name, t) in enumerate((("u_texPrev", tA), ("u_texCurr", tC), ("u_texFlow", flows[0]), ("u_texFlowPrev", flows[1]))):
            gl.glActiveTexture(gl.GL_TEXTURE0 + unit); gl.glBindTexture(gl.GL_TEXTURE_2D, t); gl.glUniform1i(L(name), unit)
        gl.glUniform1f(L("u_alpha"), 0.5); gl.glUniform1i(L("u_mode"), 0); gl.glUniform1f(L("u_sharpness"), 0.0)
        gl.glUniform1i(L("u_hudProtect"), 1); gl.glUniform2f(L("u_texelSize"), 1 / 1920, 1 / 1080)
        gl.glUniform1f(L("u_blur"), 0.0); gl.glUniform1f(L("u_frameStep"), 0.5); gl.glUniform1i(L("u_smooth"), 1)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
        px = gl.glReadPixels(0, 0, 1920, 1080, gl.GL_BGRA, gl.GL_UNSIGNED_BYTE)
        return np.flipud(np.frombuffer(px, np.uint8).reshape(1080, 1920, 4)[..., :3])

    def psnr(a, b):
        return 10 * np.log10(255 ** 2 / max(np.mean((a.astype(np.float32) - b.astype(np.float32)) ** 2), 1e-6))

    res = {"repeat": [], "blend": [], "app": []}; ms = []
    for i in range(0, len(clip) - 2, 2):
        A, B, C = clip[i], clip[i + 1], clip[i + 2]
        gA, gC = gray(A), gray(C)
        if cv2.absdiff(gA, gC).mean() > 35:  # scene cut: the app shows the real frame
            continue
        upload(tA, A); upload(tC, C)
        res["repeat"].append(psnr(A, B))
        res["blend"].append(psnr(((A.astype(np.uint16) + C) // 2).astype(np.uint8), B))
        t0 = time.perf_counter()
        f = dis.calc(gA, gC, None)
        ms.append((time.perf_counter() - t0) * 1000)
        flows.reverse()
        gl.glBindTexture(gl.GL_TEXTURE_2D, flows[0])
        gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, FW, FH, gl.GL_RG, gl.GL_FLOAT, np.ascontiguousarray(f))
        res["app"].append(psnr(render(), B))

    print(f"triplets {len(res['app'])} | flow {np.median(ms):.1f} ms")
    for k, r in res.items():
        r = np.array(r)
        print(f"{k:7s} PSNR mean {r.mean():.2f} dB | worst10% {np.sort(r)[:max(1, len(r) // 10)].mean():.2f} dB")


if __name__ == "__main__":
    main()
