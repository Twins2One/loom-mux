"""Downloaded layout must be consumable without binary-specific env overrides."""
from loom.study import asr, video


def isolate(monkeypatch, tmp_path):
    for key in ('LOOM_ASR_FFMPEG', 'BILI_VISION_FFMPEG', 'BILI_VISION_FFPROBE'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(asr, 'VENDOR', tmp_path / 'package')
    monkeypatch.setattr(asr, 'VENDOR_HOME', tmp_path / 'user')
    monkeypatch.setattr(video, 'LOOM_VENDOR', tmp_path / 'user')
    monkeypatch.setattr(video, 'DEFAULT_FFMPEG_DIR', tmp_path / 'legacy')
    dest = tmp_path / 'user' / 'ffmpeg'
    dest.mkdir(parents=True)
    for name in ('ffmpeg.exe', 'ffprobe.exe'):
        (dest / name).write_bytes(b'fixture')
    return dest


def test_downloader_and_runtime_share_custom_home(monkeypatch, tmp_path):
    from loom.config import vendor_dir
    from loom.vendor.fetch_vendor import default_dest
    monkeypatch.delenv('LOOM_VENDOR', raising=False)
    monkeypatch.setenv('LOOM_HOME', str(tmp_path / 'custom home'))
    assert default_dest() == vendor_dir() == tmp_path / 'custom home' / 'vendor'
    monkeypatch.setenv('LOOM_VENDOR', '~/loom-vendor-test')
    from pathlib import Path
    assert default_dest() == vendor_dir() == Path('~/loom-vendor-test').expanduser()


def test_legacy_and_invalid_override_semantics(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    monkeypatch.setenv('LOOM_ASR_FFMPEG', str(tmp_path / 'missing.exe'))
    monkeypatch.setenv('BILI_VISION_FFMPEG', str(tmp_path / 'missing.exe'))
    assert asr.ffmpeg() == ''
    assert video.tools()['ffmpeg'] == str(tmp_path / 'user/ffmpeg/ffmpeg.exe')
    monkeypatch.delenv('LOOM_ASR_FFMPEG')
    for name in ('ffmpeg.exe', 'ffprobe.exe'):
        (tmp_path / 'user/ffmpeg' / name).unlink()
    old = asr.VENDOR_HOME / 'bili-vision-tools/bin/ffmpeg.exe'
    old.parent.mkdir(parents=True)
    old.write_bytes(b'legacy')
    assert asr.ffmpeg() == str(old)


def test_download_layout_is_found_by_both_consumers(monkeypatch, tmp_path):
    dest = isolate(monkeypatch, tmp_path)
    assert asr.ffmpeg() == str(dest / 'ffmpeg.exe')
    found = video.tools()
    assert found['ffmpeg'] == str(dest / 'ffmpeg.exe')
    assert found['ffprobe'] == str(dest / 'ffprobe.exe')


def test_explicit_and_package_choices_still_win(monkeypatch, tmp_path):
    dest = isolate(monkeypatch, tmp_path)
    package = asr.VENDOR / 'ffmpeg'
    package.mkdir(parents=True)
    for name in ('ffmpeg.exe', 'ffprobe.exe'):
        (package / name).write_bytes(b'package')
    assert asr.ffmpeg() == str(package / 'ffmpeg.exe')
    assert video.tools()['ffmpeg'] == str(package / 'ffmpeg.exe')
    explicit = tmp_path / 'explicit.exe'
    explicit.write_bytes(b'explicit')
    monkeypatch.setenv('LOOM_ASR_FFMPEG', str(explicit))
    monkeypatch.setenv('BILI_VISION_FFMPEG', str(explicit))
    assert asr.ffmpeg() == str(explicit)
    assert video.tools()['ffmpeg'] == str(explicit)
