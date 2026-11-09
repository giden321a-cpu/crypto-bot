FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Tashkent

WORKDIR /app

COPY bot.py all_valid_symbols.json ./

EXPOSE 8080

CMD ["python", "-u", "bot.py"]
