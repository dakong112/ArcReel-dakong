import { useCallback, useEffect, useId, useRef, useState } from "react";
import { AlertTriangle, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { API } from "@/api";
import { Button } from "@/components/ui/button";
import { downloadBlob } from "@/utils/download";
import type { GetSystemVersionResponse } from "@/types";

export function AboutSection() {
  const { t } = useTranslation("dashboard");
  const [data, setData] = useState<GetSystemVersionResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const mountedRef = useRef(true);
  const versionLabelId = useId();

  useEffect(
    () => () => {
      mountedRef.current = false;
    },
    [],
  );

  const handleDownloadDiagnostics = useCallback(async () => {
    if (!mountedRef.current) return;
    setDownloading(true);
    setDownloadError(null);
    try {
      const { blob, filename } = await API.downloadDiagnostics();
      downloadBlob(blob, filename);
    } catch (err) {
      if (!mountedRef.current) return;
      setDownloadError(err instanceof Error ? err.message : String(err));
    } finally {
      if (mountedRef.current) {
        setDownloading(false);
      }
    }
  }, []);

  const fetchVersion = useCallback(async () => {
    setError(null);
    try {
      const result = await API.getSystemVersion();
      if (mountedRef.current) setData(result);
    } catch (err) {
      if (mountedRef.current) {
        setError(err instanceof Error ? err.message : t("about_load_failed"));
      }
    } finally {
      if (mountedRef.current) {
        setLoading(false);
      }
    }
  }, [t]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- mount 后异步拉取版本，回调内回写状态
    void fetchVersion();
  }, [fetchVersion]);

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="size-4 text-primary animate-spin" aria-hidden />
        {t("about_loading")}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <h2 className="text-lg font-medium">{t("about")}</h2>

      {/* 本 fork 不检查上游新版本，也不展示上游发布说明；只显示当前版本。 */}
      <section aria-labelledby={versionLabelId} className="flex flex-col gap-2 rounded-lg border border-border bg-card p-4">
        <p id={versionLabelId} className="text-xs font-medium text-muted-foreground">
          {t("about_current_version")}
        </p>
        <span className="text-2xl font-medium tabular-nums">{data?.current.version ?? "-"}</span>
        {error && (
          <p role="alert" className="flex items-start gap-2 rounded-md bg-warn/10 px-3 py-2 text-sm text-warn">
            <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0" />
            <span className="min-w-0 break-words">{error}</span>
          </p>
        )}
      </section>

      <section className="flex flex-col items-start gap-3 rounded-lg border border-border bg-card p-4">
        <div className="flex flex-col gap-1">
          <h3 className="text-sm font-medium">{t("diagnostics_section_title")}</h3>
          <p className="text-sm text-muted-foreground">{t("diagnostics_section_desc")}</p>
        </div>
        <Button variant="outline" onClick={() => void handleDownloadDiagnostics()} disabled={downloading}>
          {downloading ? t("diagnostics_downloading") : t("diagnostics_download")}
        </Button>
        {downloadError && (
          <p role="alert" className="text-sm text-destructive">
            {t("diagnostics_download_failed", { error: downloadError })}
          </p>
        )}
      </section>

      {/* Copyright & attribution — NOTICE §7(b) 要求的署名句与仓库链接，逐字保留，不走品牌占位 */}
      <section className="flex flex-col gap-2 rounded-lg border border-border bg-card p-4">
        <h3 className="text-sm font-medium">{t("about_legal_title")}</h3>
        <div className="flex flex-col gap-1 text-sm text-muted-foreground">
          <p>Copyright © 2026 Pollo3470 and ArcReel contributors</p>
          <p>
            Powered by ArcReel —{" "}
            <a
              href="https://github.com/ArcReel/ArcReel"
              target="_blank"
              rel="noreferrer"
              className="break-all text-primary underline-offset-4 hover:underline"
            >
              https://github.com/ArcReel/ArcReel
            </a>
          </p>
          <p>
            {t("about_source_offer")}
            <a
              href="https://github.com/dakong112/ArcReel-dakong"
              target="_blank"
              rel="noreferrer"
              className="break-all text-primary underline-offset-4 hover:underline"
            >
              https://github.com/dakong112/ArcReel-dakong
            </a>
          </p>
        </div>
      </section>
    </div>
  );
}
