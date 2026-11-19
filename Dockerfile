FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-liberation ca-certificates tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir --only-binary=llama-cpp-python \
      --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu "llama-cpp-python==0.3.36" huggingface_hub
ENV SOCIAL_TITLE_MODEL=/opt/clipmatch/Qwen3.5-2B-Q4_K_M.gguf
RUN HF_HUB_DISABLE_XET=1 python -c "from huggingface_hub import snapshot_download; from llama_cpp import Llama, mtmd_cpp; snapshot_download('unsloth/Qwen3.5-2B-GGUF', revision='f6d5376be1edb4d416d56da11e5397a961aca8ae', allow_patterns=['Qwen3.5-2B-Q4_K_M.gguf','mmproj-F16.gguf'], local_dir='/opt/clipmatch', max_workers=1)"
COPY pyproject.toml .
COPY clipmatch/ clipmatch/
RUN python -m pip install --no-cache-dir ".[live,transcription]"
ENV SOCIAL_SPEECH_MODEL=/opt/clipmatch/tiny.en
RUN python -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-whisper-tiny.en', revision='0d3d19a32d3338f10357c0889762bd8d64bbdeba', allow_patterns=['config.json','model.bin','tokenizer.json','vocabulary.txt'], local_dir='/opt/clipmatch/tiny.en')"
CMD ["clipmatch-live", "watch"]
