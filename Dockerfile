FROM python:3.12-slim
WORKDIR /srv/mafutani-tcc
COPY . /srv/mafutani-tcc
ENV PORT=8000
EXPOSE 8000
CMD ["python3", "app.py"]
