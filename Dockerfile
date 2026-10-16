FROM python:3.12-slim
WORKDIR /srv/mafutani-cta
COPY . /srv/mafutani-cta
ENV PORT=8000
EXPOSE 8000
CMD ["python3", "app.py"]
