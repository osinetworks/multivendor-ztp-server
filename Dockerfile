FROM alpine:3.22

# Install all dependencies via apk (Alpine native packages - no pip needed)
RUN apk add --no-cache \
    dnsmasq \
    python3 \
    py3-flask \
    py3-requests \
    py3-yaml \
    py3-gunicorn \
    tini \
    bash \
    iproute2 \
    net-tools \
    curl

# Copy dnsmasq config template — rendered to /etc/dnsmasq.conf at container startup
# by entrypoint.sh using env vars from .env via docker-compose
COPY config/dnsmasq.conf.template /etc/dnsmasq.conf.template

# Copy ZTP server application
COPY scripts/ztp_server.py /usr/local/bin/ztp_server.py
COPY scripts/inventory_manager.py /usr/local/bin/inventory_manager.py

# Setup working directory
WORKDIR /var/www/ztp

# Copy bootstrap scripts (served to switches by vendor)
COPY scripts/bootstrap_arista /var/www/ztp/bootstrap_arista
COPY scripts/bootstrap_cisco  /var/www/ztp/bootstrap_cisco
# Legacy symlink — keeps old URL /bootstrap working for Arista
RUN ln -sf /var/www/ztp/bootstrap_arista /var/www/ztp/bootstrap

# Copy entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh /usr/local/bin/ztp_server.py /usr/local/bin/inventory_manager.py \
    /var/www/ztp/bootstrap_arista /var/www/ztp/bootstrap_cisco

# Volumes for persistent data
VOLUME ["/var/www/ztp/configs", "/var/www/ztp/firmware", "/var/www/ztp/logs"]

# HTTP server port
EXPOSE 8080

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["/entrypoint.sh"]