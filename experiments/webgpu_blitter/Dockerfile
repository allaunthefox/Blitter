FROM archlinux@sha256:f0703c5a076af362f1cb99a01d1d0670fa63f16f5aeb74881b7ded9b63eea0c1
RUN pacman -Syu --noconfirm vulkan-radeon vulkan-swrast python bash && pacman -Scc --noconfirm
COPY blitter-daemon /usr/local/bin/blitter-daemon
COPY lease_gate.py /usr/local/libexec/blitter-lease-gate.py
COPY blitter-stack-entrypoint.sh /usr/local/bin/blitter-stack-entrypoint
RUN chmod 0755 \
    /usr/local/bin/blitter-daemon \
    /usr/local/bin/blitter-stack-entrypoint \
    /usr/local/libexec/blitter-lease-gate.py
EXPOSE 8790
ENTRYPOINT ["/usr/local/bin/blitter-stack-entrypoint"]
