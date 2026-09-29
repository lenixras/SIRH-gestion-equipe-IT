# GLPI simplifie pour les tests : l'API REST v1, sans PHP ni MariaDB.
#   docker build -t sirh-glpi-sim mock-glpi
#   docker run --rm -p 8081:8081 -e MOCK_GLPI_APP_TOKEN=simtoken sirh-glpi-sim
FROM python:3.13-alpine
WORKDIR /srv
COPY app.py .
ENV MOCK_GLPI_PORT=8081
EXPOSE 8081
# ponytail: aucune dependance (http.server stdlib) -> image de depart legere, demarrage instantane
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8081/glpi/apirest.php').read()"
CMD ["python", "app.py"]
