# Built outside the air gap, then imported into the internal registry.
# UBI runs as a non-root user with group 0 write access, which fits OpenShift's
# random-UID security context.
FROM registry.access.redhat.com/ubi9/python-311:latest

WORKDIR /opt/app-root/src
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=1001:0 . .
ENV DJANGO_DEBUG=false
RUN DJANGO_SECRET_KEY=build-only python manage.py collectstatic --noinput

EXPOSE 8080
# The same image runs the web UI (default) and the CronJobs, e.g.
#   python manage.py collect --backfill-hours 6
#   python manage.py compute_recommendations
CMD ["gunicorn", "houdini.wsgi", "--bind", "0.0.0.0:8080", "--workers", "3", "--timeout", "120"]
