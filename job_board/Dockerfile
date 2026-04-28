FROM python:3.12-slim

WORKDIR /app

COPY job_board/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY job_board/app.py /app/app.py

EXPOSE 5060

ENV JOB_BOARD_PORT=5060
CMD ["python", "app.py"]
