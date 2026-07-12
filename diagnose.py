# ═══════════════════════════════════════════════════
# 诊断脚本 · diagnose.py
# 单独作为一个 Streamlit app 部署（换个仓库或主文件名），
# 或本地 streamlit run diagnose.py 跑。
# 用来定位到底是哪一层出问题：TTS？内存？会话状态？依赖？
# 每一步都是独立按钮，出问题时能明确指出是哪步崩的。
# ═══════════════════════════════════════════════════
import streamlit as st
import sys
import os
import platform
import asyncio
import time
import io
import gc
import traceback

st.set_page_config(page_title="🔬 诊断", page_icon="🔬", layout="centered")
st.title("🔬 日语听力练习 · 系统诊断")
st.caption("每步按钮独立，出问题时你能看到具体崩在哪。")


# ── 1. 环境信息 ──────────────────────────────────
st.header("1️⃣ 环境信息")
if st.button("📊 显示环境", use_container_width=True):
    info = {
        "Python 版本":     sys.version.split()[0],
        "平台":            platform.platform(),
        "工作目录":         os.getcwd(),
        "Streamlit 版本":   st.__version__,
    }
    for pkg in ("edge_tts", "aiohttp", "requests", "pandas", "numpy"):
        try:
            m = __import__(pkg)
            info[f"{pkg} 版本"] = getattr(m, '__version__', '?')
        except ImportError:
            info[f"{pkg} 版本"] = "❌ 未安装"
    st.json(info)

    try:
        import resource
        u = resource.getrusage(resource.RUSAGE_SELF)
        st.write("**资源使用**")
        st.write(f"- 最大 RSS: {u.ru_maxrss // 1024} MB")
        st.write(f"- 用户 CPU: {u.ru_utime:.2f}s")
        st.write(f"- 系统 CPU: {u.ru_stime:.2f}s")
    except Exception as e:
        st.caption(f"resource 模块不可用: {e}")

st.divider()


# ── 2. TTS 单次生成 ──────────────────────────────
st.header("2️⃣ TTS 单次生成（asyncio.run）")
st.caption("如果这步崩了，就是 edge-tts 或 asyncio 层的问题。")

async def _tts_async(word, voice="ja-JP-NanamiNeural", rate="+0%"):
    import edge_tts
    com = edge_tts.Communicate(word, voice, rate=rate)
    buf = io.BytesIO()
    async for chunk in com.stream():
        if chunk["type"] == "audio":
            buf.write(chunk["data"])
    return buf.getvalue()

if st.button("🎵 生成一次「食べる」", use_container_width=True):
    try:
        t0 = time.time()
        data = asyncio.run(_tts_async("食べる"))
        dt = time.time() - t0
        st.success(f"✅ 成功  ·  {len(data)//1024} KB  ·  {dt:.2f}s")
        st.audio(data, format="audio/mpeg")
    except Exception as e:
        st.error(f"❌ 失败: {e}")
        st.code(traceback.format_exc())

st.divider()


# ── 3. TTS 高频调用（模拟练习翻页）─────────────────
st.header("3️⃣ TTS 高频调用 × 10")
st.caption("反复调用 10 次，模拟练习中快速翻词。这步是 socket 泄漏最容易暴露的地方。")

n_hits = st.number_input("次数", min_value=1, max_value=50, value=10)
if st.button(f"🔁 连续生成 {n_hits} 次", use_container_width=True):
    words = ["食べる", "飲む", "きれい", "勉強する", "図書館",
             "電車", "天気", "友達", "学校", "先生"]
    ok = fail = 0
    total_bytes = 0
    t0 = time.time()
    prog = st.progress(0.0)
    for i in range(n_hits):
        try:
            data = asyncio.run(_tts_async(words[i % len(words)]))
            if data:
                ok += 1
                total_bytes += len(data)
            else:
                fail += 1
        except Exception as e:
            fail += 1
            st.warning(f"第 {i+1} 次失败: {e}")
        prog.progress((i+1) / n_hits)
    prog.empty()
    dt = time.time() - t0
    st.success(f"✅ 完成 {ok} 成功 / {fail} 失败  ·  总耗时 {dt:.1f}s  ·  共 {total_bytes//1024} KB")

    try:
        import resource
        u = resource.getrusage(resource.RUSAGE_SELF)
        st.write(f"当前 RSS: **{u.ru_maxrss // 1024} MB**")
    except Exception:
        pass

st.divider()


# ── 4. 批量并发 ────────────────────────────────
st.header("4️⃣ 批量并发（模拟循环朗读音频生成）")
st.caption("gather 并发多个任务，这里控制并发数。")

conc = st.slider("并发数", 1, 15, 6)
batch_n = st.slider("批量任务数", 1, 30, 12)

async def _batch(n, concurrency):
    sem = asyncio.Semaphore(concurrency)
    words = ["食べる", "飲む", "きれい"] * (n // 3 + 1)
    words = words[:n]
    async def _one(w):
        async with sem:
            return await _tts_async(w)
    r = await asyncio.gather(*[_one(w) for w in words], return_exceptions=True)
    ok = sum(1 for x in r if isinstance(x, (bytes, bytearray)) and x)
    return ok

if st.button(f"🌊 并发生成 {batch_n} 个（并发 {conc}）", use_container_width=True):
    try:
        t0 = time.time()
        ok = asyncio.run(_batch(batch_n, conc))
        dt = time.time() - t0
        st.success(f"✅ {ok}/{batch_n} 成功  ·  {dt:.1f}s  ·  平均 {dt/max(batch_n,1):.2f}s/条")
    except Exception as e:
        st.error(f"❌ 失败: {e}")
        st.code(traceback.format_exc())

st.divider()


# ── 5. 会话状态检查 ────────────────────────────
st.header("5️⃣ Session State 大小")
st.caption("如果这里显示 >10 MB，说明 session_state 里存了大对象（比如 loop_audio）。")

if st.button("📦 显示 session_state 大小", use_container_width=True):
    import pickle
    total = 0
    breakdown = {}
    for k, v in st.session_state.items():
        try:
            size = len(pickle.dumps(v))
        except Exception:
            size = -1
        breakdown[k] = f"{size / 1024:.1f} KB" if size >= 0 else "无法计算"
        if size > 0:
            total += size
    st.metric("总大小", f"{total / 1024 / 1024:.2f} MB")
    st.json(breakdown)

st.divider()


# ── 6. Gist 网络 ───────────────────────────────
st.header("6️⃣ Gist 网络连通")
if st.button("🌐 测试 GitHub 连通性", use_container_width=True):
    import requests
    try:
        r = requests.get("https://api.github.com", timeout=10)
        st.success(f"✅ GitHub API 可达  ·  状态码 {r.status_code}")
    except Exception as e:
        st.error(f"❌ 无法连接 GitHub: {e}")

st.divider()


# ── 7. 强制 GC ─────────────────────────────────
st.header("7️⃣ 强制 GC + 内存回收")
if st.button("♻️ 立即 GC", use_container_width=True):
    n = gc.collect()
    st.info(f"回收了 {n} 个对象")
    try:
        import resource
        u = resource.getrusage(resource.RUSAGE_SELF)
        st.write(f"GC 后 RSS: **{u.ru_maxrss // 1024} MB**")
    except Exception:
        pass


st.divider()
st.caption("💡 用法：按顺序点每个按钮，看哪一步开始出错。把日志发给 Claude 帮你分析。")
