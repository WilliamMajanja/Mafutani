FROM python:3.12-slim
WORKDIR /srv/Mafutani
COPY . /srv/Mafutani
ENV PORT=8000
EXPOSE 8000
CMD ["python3", "app.py"]
