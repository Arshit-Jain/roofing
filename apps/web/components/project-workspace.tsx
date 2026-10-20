"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ArrowLeft, Check, Download, FileCheck2, House, LoaderCircle, Plus, Upload } from "lucide-react";
import type { User } from "@firebase/auth";
import { useAuth } from "@/components/auth-provider";
import { LoginForm } from "@/components/login-form";
import { ApiError, apiRequest, downloadWithAuth, uploadPdf } from "@/lib/api";
import type { AppConfig, EstimateResult, GeometryVersion, Job, Material, MeasurementVersion, Project, ProjectStatus, Quantity, Report } from "@/lib/domain";
import { firebaseConfigured } from "@/lib/firebase";
import { GeometryReviewPanel } from "@/components/geometry-review-panel";

const ProjectRoofViewer = dynamic(() => import("@/components/roof/ProjectRoofViewer").then((module) => module.ProjectRoofViewer), { ssr: false, loading: () => <div className="card empty"><p className="muted">Loading project roof…</p></div> });

const tabs = ["Overview", "Measurements", "Geometry review", "3D roof", "Materials & pricing", "Estimate", "Quote"] as const;
type Tab = (typeof tabs)[number];

type WorkspaceSnapshot = {
  project: Project;
  config: AppConfig;
  reports: Report[];
  measurements: MeasurementVersion | null;
  geometry: GeometryVersion | null;
};

async function fetchWorkspace(user: User, projectId: string): Promise<WorkspaceSnapshot> {
  const [project, config, reports] = await Promise.all([
    apiRequest<Project>(user, `/api/projects/${encodeURIComponent(projectId)}`),
    apiRequest<AppConfig>(user, "/api/config"),
    apiRequest<Report[]>(user, `/api/projects/${encodeURIComponent(projectId)}/reports`),
  ]);
  const measurements = project.currentMeasurementVersionId
    ? await apiRequest<MeasurementVersion>(
        user,
        `/api/projects/${encodeURIComponent(projectId)}/measurements`,
      )
    : null;
  let geometry: GeometryVersion | null = null;
  if (project.currentGeometryVersionId) {
    try {
      geometry = await apiRequest<GeometryVersion>(user, `/api/projects/${encodeURIComponent(projectId)}/geometry`);
    } catch (cause) {
      if (!(cause instanceof ApiError) || cause.status !== 404) throw cause;
    }
  }
  return { project, config, reports, measurements, geometry };
}

function metricValue(values: Record<string, Quantity> | undefined, key: string, digits = 0): string {
  const quantity = values?.[key];
  if (!quantity) return "—";
  if (quantity.state === "not_surveyed") return "Not surveyed";
  if (quantity.state !== "measured" && quantity.state !== "zero") return "Not reported";
  const number = Number(quantity.value ?? 0);
  return `${number.toLocaleString(undefined, { maximumFractionDigits: digits })} ${quantity.unit}`;
}

function statusBadge(status: ProjectStatus) {
  if (status === "needs_review") return <span className="badge orange">Review measurements</span>;
  if (status === "ready") return <span className="badge green">Ready to estimate</span>;
  if (status === "failed") return <span className="badge red">Needs attention</span>;
  if (status === "processing") return <span className="badge">Processing</span>;
  return <span className="badge">Report needed</span>;
}

function WorkspaceContent({ user, projectId }: { user: User; projectId: string }) {
  const { signOutUser } = useAuth();
  const [project, setProject] = useState<Project | null>(null);
  const [measurements, setMeasurements] = useState<MeasurementVersion | null>(null);
  const [geometryVersion, setGeometryVersion] = useState<GeometryVersion | null>(null);
  const [appConfig, setAppConfig] = useState<AppConfig | null>(null);
  const [reports, setReports] = useState<Report[]>([]);
  const [estimateMaterials, setEstimateMaterials] = useState<Material[]>([]);
  const [activeTab, setActiveTab] = useState<Tab>("Overview");
  const [progress, setProgress] = useState<number | null>(null);
  const [jobMessage, setJobMessage] = useState("");
  const [processingJob, setProcessingJob] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [workspaceLoading, setWorkspaceLoading] = useState(true);

  const loadWorkspace = useCallback(async () => {
    try {
      const snapshot = await fetchWorkspace(user, projectId);
      setProject(snapshot.project);
      setAppConfig(snapshot.config);
      setReports(snapshot.reports);
      setMeasurements(snapshot.measurements);
      setGeometryVersion(snapshot.geometry);
      setError("");
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Could not load this project.");
    }
  }, [projectId, user]);

  const hasActiveExtraction = reports.some((report) =>
    ["queued", "processing"].includes(report.extractionStatus),
  );
  const hasActiveGeometry = project?.geometryStatus === "processing";
  const isProcessing = busy || hasActiveExtraction || hasActiveGeometry;
  const visibleJobMessage = (hasActiveGeometry && processingJob?.progress?.message) || jobMessage || (
    hasActiveGeometry
      ? "Building the editable roof draft from the PDF…"
      : hasActiveExtraction
        ? "Extracting measurements from the PDF…"
        : ""
  );

  async function watchExtraction(jobId: string, reportId?: string) {
    const intervalSeconds = appConfig?.jobPollIntervalSeconds ?? 2;
    const timeoutSeconds = appConfig?.jobPollTimeoutSeconds ?? 180;
    const deadline = Date.now() + timeoutSeconds * 1000;
    let completed = false;
    while (Date.now() < deadline) {
      const [job, currentReports] = await Promise.all([
        apiRequest<Job>(user, `/api/jobs/${encodeURIComponent(jobId)}`),
        apiRequest<Report[]>(user, `/api/projects/${encodeURIComponent(projectId)}/reports`),
      ]);
      setReports(currentReports);
      if (job.status === "succeeded") {
        setJobMessage("Measurements saved. Building the editable roof draft from the PDF…");
        completed = true;
        if (reportId) await watchGeometry(reportId, intervalSeconds, timeoutSeconds);
        break;
      }
      if (job.status === "failed") {
        const diagnostic = [job.error?.stage, job.error?.operation].filter(Boolean).join(" / ");
        setJobMessage(
          [
            job.error?.message || "Extraction failed. Review the report status.",
            diagnostic ? `Stage: ${diagnostic}` : "",
          ]
            .filter(Boolean)
            .join(" · "),
        );
        completed = true;
        break;
      }
      await new Promise((resolve) => window.setTimeout(resolve, intervalSeconds * 1000));
    }
    if (!completed) setJobMessage("Extraction is still running. You can leave this page and check again later.");
    await loadWorkspace();
  }

  async function watchGeometry(reportId: string, intervalSeconds: number, timeoutSeconds: number) {
    const deadline = Date.now() + timeoutSeconds * 1000;
    while (Date.now() < deadline) {
      await new Promise((resolve) => window.setTimeout(resolve, intervalSeconds * 1000));
      try {
        const snapshot = await fetchWorkspace(user, projectId);
        setProject(snapshot.project);
        setAppConfig(snapshot.config);
        setReports(snapshot.reports);
        setMeasurements(snapshot.measurements);
        setGeometryVersion(snapshot.geometry);
        if (snapshot.geometry?.sourceReportId === reportId && snapshot.geometry.status !== "processing") {
          setJobMessage(snapshot.geometry.status === "failed"
            ? "Measurements were saved. Roof reconstruction needs attention; open Geometry review to inspect and retry."
            : "Measurements and a PDF-derived roof draft are ready for review.");
          return;
        }
        if (snapshot.project.geometryStatus === "failed") {
          setJobMessage("Measurements were saved. Roof reconstruction needs attention; open Geometry review to inspect and retry.");
          return;
        }
      } catch (cause) {
        setJobMessage(cause instanceof Error
          ? `Measurements were saved. ${cause.message}`
          : "Measurements were saved. Reload the project to check roof reconstruction.");
        return;
      }
    }
    setJobMessage("Measurements were saved. Roof reconstruction is still running; you can review measurements while it finishes.");
  }

  useEffect(() => {
    let active = true;
    void fetchWorkspace(user, projectId)
      .then((snapshot) => {
        if (!active) return;
        setProject(snapshot.project);
        setAppConfig(snapshot.config);
        setReports(snapshot.reports);
        setMeasurements(snapshot.measurements);
        setGeometryVersion(snapshot.geometry);
        setError("");
      })
      .catch((cause: unknown) => {
        if (active) {
          setError(cause instanceof ApiError ? cause.message : "Could not load this project.");
        }
      })
      .finally(() => {
        if (active) setWorkspaceLoading(false);
      });
    return () => { active = false; };
  }, [projectId, user]);

  useEffect(() => {
    if (!hasActiveExtraction || busy) return;
    let active = true;
    let timeout: number | undefined;
    const poll = async () => {
      await loadWorkspace();
      if (active) {
        timeout = window.setTimeout(
          () => void poll(),
          (appConfig?.jobPollIntervalSeconds ?? 2) * 1000,
        );
      }
    };
    timeout = window.setTimeout(() => void poll(), (appConfig?.jobPollIntervalSeconds ?? 2) * 1000);
    return () => {
      active = false;
      if (timeout !== undefined) window.clearTimeout(timeout);
    };
  }, [appConfig?.jobPollIntervalSeconds, busy, hasActiveExtraction, loadWorkspace]);

  useEffect(() => {
    if (!hasActiveGeometry || busy) return;
    let active = true;
    let timeout: number | undefined;
    const poll = async () => {
      await loadWorkspace();
      if (active) {
        timeout = window.setTimeout(
          () => void poll(),
          (appConfig?.jobPollIntervalSeconds ?? 2) * 1000,
        );
      }
    };
    timeout = window.setTimeout(() => void poll(), (appConfig?.jobPollIntervalSeconds ?? 2) * 1000);
    return () => {
      active = false;
      if (timeout !== undefined) window.clearTimeout(timeout);
    };
  }, [appConfig?.jobPollIntervalSeconds, busy, hasActiveGeometry, loadWorkspace]);

  useEffect(() => {
    const jobId = geometryVersion?.jobId;
    if (!jobId) {
      setProcessingJob(null);
      return;
    }
    let active = true;
    let timeout: number | undefined;
    const poll = async () => {
      try {
        const job = await apiRequest<Job>(user, `/api/jobs/${encodeURIComponent(jobId)}`);
        if (!active) return;
        setProcessingJob(job);
        if (job.status === "succeeded" || job.status === "failed") return;
      } catch {
        // Keep the last available progress log visible during transient polling errors.
      }
      if (active) {
        timeout = window.setTimeout(
          () => void poll(),
          (appConfig?.jobPollIntervalSeconds ?? 2) * 1000,
        );
      }
    };
    void poll();
    return () => {
      active = false;
      if (timeout !== undefined) window.clearTimeout(timeout);
    };
  }, [appConfig?.jobPollIntervalSeconds, geometryVersion?.jobId, user]);

  async function submitPdf(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".pdf") && file.type !== "application/pdf") {
      setError("Choose a PDF measurement report.");
      return;
    }
    setError("");
    setJobMessage("Uploading measurement report…");
    setProgress(0);
    setBusy(true);
    try {
      const result = await uploadPdf(user, projectId, file, setProgress);
      setProgress(null);
      await loadWorkspace();
      if (result.jobId) {
        setJobMessage("Report stored. Extracting measurements…");
        await watchExtraction(result.jobId, result.id);
      } else {
        setJobMessage(result.duplicate ? "This PDF is already stored in the project." : "PDF upload complete.");
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "PDF upload failed.");
      setJobMessage("");
    } finally {
      setBusy(false);
      setProgress(null);
    }
  }

  async function retryExtraction(reportId: string) {
    try {
      setError("");
      setBusy(true);
      setJobMessage("Retrying measurement extraction…");
      const result = await apiRequest<{ jobId: string; status: string }>(
        user,
        `/api/projects/${encodeURIComponent(projectId)}/reports/${encodeURIComponent(reportId)}/extract`,
        { method: "POST" },
      );
      await watchExtraction(result.jobId, reportId);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not retry extraction.");
    } finally {
      setBusy(false);
    }
  }

  async function downloadReport(reportId: string) {
    try {
      const blob = await downloadWithAuth(user, `/api/projects/${encodeURIComponent(projectId)}/reports/${encodeURIComponent(reportId)}/file`);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = reports.find((report) => report.id === reportId)?.originalFilename || "roof-report.pdf";
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Report download failed."); }
  }

  async function saveMaterialSelection(key: string, value: string) {
    if (!project) return;
    try {
      const updated = await apiRequest<Project>(user, `/api/projects/${encodeURIComponent(projectId)}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ materialSelections: { ...(project.materialSelections ?? {}), [key]: value } }),
      });
      setProject(updated);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Could not save the appearance choice."); }
  }

  function toggleEstimateMaterial(material: Material) {
    setEstimateMaterials((current) => current.some((item) => item.id === material.id)
      ? current.filter((item) => item.id !== material.id)
      : [...current, material]);
  }

  function updateEstimateMaterial(material: Material) {
    setEstimateMaterials((current) => current.map((item) => item.id === material.id ? material : item));
  }

  if (workspaceLoading && !project) return <div className="card empty"><p className="muted">Loading project…</p></div>;
  if (!project) return <div className="page"><div className="error">{error || "Project not found."}</div><Link href="/" className="button secondary"><ArrowLeft size={15} />Dashboard</Link></div>;
  const values = measurements?.values;
  const latestReport = reports[0];

  return <div className="shell">
    <header className="topbar"><Link href="/" className="brand"><span className="brand-mark"><House size={18} /></span><span>ROOFLINE</span></Link><div className="top-actions"><span>{user.displayName || user.email}</span><button className="button secondary small" onClick={() => void signOutUser()}>Sign out</button></div></header>
    <main className="page">
      <Link href="/" className="row muted" style={{ fontSize: 12, marginBottom: 20 }}><ArrowLeft size={14} />All projects</Link>
      <div className="workspace-header"><div><div className="eyebrow">Project workspace</div><h1>{project.name}</h1><div className="subcopy">{project.address || "Property address not added"}</div></div><div className="row">{statusBadge(project.status)}<label className="button"><Upload size={15} />Upload report<input type="file" accept="application/pdf,.pdf" onChange={(event) => void submitPdf(event)} disabled={busy} hidden /></label></div></div>
      {error && <div className="error" role="alert">{error}</div>}
      {(visibleJobMessage || isProcessing) && <div className={latestReport?.extractionStatus === "failed" ? "error" : "notice green-notice"} style={{ marginTop: 17 }} role={latestReport?.extractionStatus === "failed" ? "alert" : "status"} aria-live="polite">
        <span className="row">{isProcessing && <LoaderCircle size={16} className="spin" aria-hidden="true" />}{visibleJobMessage}</span>
        {progress !== null && <><div className="progress-track"><div className="progress-fill" style={{ width: `${progress}%` }} /></div><span style={{ fontSize: 11 }}>{progress}% uploaded</span></>}
        {isProcessing && progress === null && <div className="muted" style={{ fontSize: 11, marginTop: 6 }}>This can take a few minutes. You can keep using other project tabs while it runs.</div>}
      </div>}
      {geometryVersion?.jobId && <details className="card processing-log" style={{ marginTop: 12, padding: "12px 15px" }}>
        <summary>View processing log{processingJob?.events?.length ? ` (${processingJob.events.length})` : ""}</summary>
        <div className="processing-log-body">
          <div><b>Status:</b> {processingJob?.status ?? "Loading…"}</div>
          {processingJob?.progress && <div style={{ marginTop: 4 }}><b>Current step:</b> {processingJob.progress.message}</div>}
          {processingJob?.events?.length ? <ol className="processing-log-list">
            {processingJob.events.map((event, index) => <li key={`${event.at ?? event.stage}-${index}`}>
              <span className={`processing-log-level ${event.level ?? "info"}`}>{event.level === "warning" ? "Warning" : event.level === "error" ? "Error" : "Info"}</span>
              {event.at && <time dateTime={event.at}>{new Date(event.at).toLocaleTimeString()}</time>}
              <span>{event.message}</span>
            </li>)}
          </ol> : <p className="muted" style={{ margin: "7px 0 0" }}>No step updates have been recorded for this run yet. Check the API server terminal for details.</p>}
          {processingJob?.error?.message && <div className="error" style={{ margin: "8px 0 0" }}>{processingJob.error.message}</div>}
        </div>
      </details>}
      {latestReport && <div className="card panel" style={{ marginTop: 18, padding: 15 }}><div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap" }}><div className="row"><FileCheck2 color="#0d6a58" size={18} /><div><b style={{ fontSize: 13 }}>{latestReport.originalFilename}</b><div className="muted" style={{ fontSize: 11 }}>Storage: Firestore chunks · extraction: {latestReport.extractionStatus}</div></div></div><div className="row"><button className="button secondary small" onClick={() => void downloadReport(latestReport.id)}><Download size={14} />Download</button><button className="button secondary small" disabled={busy || ["queued", "processing"].includes(latestReport.extractionStatus)} onClick={() => void retryExtraction(latestReport.id)}>Retry extraction</button></div></div>{latestReport.extractionStatus === "failed" && <div className="error" style={{ marginBottom: 0 }}>{latestReport.extractionError || "Extraction failed. Check the report details and retry."}{latestReport.extractionErrorCode && <div className="muted" style={{ fontSize: 11, marginTop: 5 }}>Code: {latestReport.extractionErrorCode}{latestReport.extractionErrorStage && ` · Stage: ${latestReport.extractionErrorStage}`}{latestReport.extractionErrorOperation && ` · Step: ${latestReport.extractionErrorOperation}`}</div>}</div>}</div>}
      <nav className="tabs" aria-label="Project workspace sections">{tabs.map((tab) => <button key={tab} className={`tab ${activeTab === tab ? "active" : ""}`} onClick={() => setActiveTab(tab)}>{tab}</button>)}</nav>
      {activeTab === "Overview" && <Overview project={project} values={values} version={measurements} pitch={measurements?.predominantPitchNotation} />}
      {activeTab === "Measurements" && <MeasurementsPanel user={user} projectId={projectId} version={measurements} onSaved={async () => { await loadWorkspace(); }} />}
      {activeTab === "Geometry review" && <GeometryReviewPanel key={geometryVersion?.id ?? "no-geometry"} user={user} projectId={projectId} reportId={project.currentReportId ?? latestReport?.id} initialVersion={geometryVersion} onVersionChange={setGeometryVersion} onReload={loadWorkspace} />}
      {activeTab === "3D roof" && <ProjectRoofViewer key={project.id} version={geometryVersion} user={user} projectId={projectId} geometryStatus={project.geometryStatus} selections={project.materialSelections ?? {}} onSelectionChange={(key, value) => void saveMaterialSelection(key, value)} onVersionChange={setGeometryVersion} onReload={loadWorkspace} />}
      {activeTab === "Materials & pricing" && <MaterialsPanel user={user} config={appConfig} selectedMaterials={estimateMaterials} onToggleEstimateMaterial={toggleEstimateMaterial} onMaterialSaved={updateEstimateMaterial} />}
      {activeTab === "Estimate" && <EstimatePanel user={user} projectId={projectId} values={values} version={measurements} config={appConfig} selectedMaterials={estimateMaterials} />}
      {activeTab === "Quote" && <div className="card panel"><h2>Quote preparation</h2><p className="muted">{estimateMaterials.length > 0 ? `The estimate will include only the ${estimateMaterials.length} material${estimateMaterials.length === 1 ? "" : "s"} added with + in Materials & pricing, with quantities calculated from the measured roof area.` : "Add materials with + in Materials & pricing to include selected catalog items in your estimate."} Save the estimate from the Estimate tab to attach its quantities and waste allowances to this quote draft. Customer-ready quote PDF generation is not available yet.</p></div>}
    </main>
  </div>;

}

function formatFeet(value: string | null | undefined): string {
  if (value === undefined || value === null || value === "") return "—";
  const inchesTotal = Math.round(Number(value) * 12);
  const feet = Math.floor(inchesTotal / 12);
  const inches = inchesTotal % 12;
  return `${feet}' ${inches}"`;
}

function Overview({ project, values, version, pitch }: { project: Project; values?: Record<string, Quantity>; version: MeasurementVersion | null; pitch?: string | null }) {
  const isRoofr = version?.sourceFormat === "roofr";
  return <div>
    {!values && <div className="notice" style={{ marginBottom: 17 }}>Upload a Roof Manager or Roofr measurement PDF to extract and review measurements. No roof shape is inferred from project name or address.</div>}
    <div className="grid metric-grid">
      <Metric value={metricValue(values, "roofAreaSqFt")} label="Total roof area" />
      {isRoofr && <><Metric value={metricValue(values, "pitchedRoofAreaSqFt")} label="Pitched roof" /><Metric value={metricValue(values, "flatRoofAreaSqFt")} label="Flat roof" /></>}
      <Metric value={pitch || "—"} label="Predominant pitch" />
      <Metric value={metricValue(values, "roofPlaneCount")} label={isRoofr ? "Roof facets" : "Reported roof planes"} />
      <Metric value={isRoofr ? formatFeet(values?.eavesLf?.value) : metricValue(values, "eavesLf")} label="Eaves" />
      <Metric value={isRoofr ? formatFeet(values?.ridgesLf?.value) : metricValue(values, "ridgeLf")} label="Ridges" />
      <Metric value={isRoofr ? formatFeet(values?.hipsLf?.value) : metricValue(values, "hipsLf")} label="Hips" />
      <Metric value={isRoofr ? formatFeet(values?.rakesLf?.value) : metricValue(values, "rakesLf")} label="Rakes" />
      <Metric value={isRoofr ? formatFeet(values?.valleysLf?.value) : metricValue(values, "valleysLf")} label="Valleys" />
      {isRoofr && <Metric value={formatFeet(values?.transitionsLf?.value)} label="Transitions" />}
    </div>
    {project.status === "failed" && <div className="error" style={{ marginTop: 16 }}>The latest processing step failed. Check the report status and retry when the extraction service is configured.</div>}
  </div>;
}

function Metric({ value, label }: { value: string; label: string }) {
  return <div className="card metric"><div className="metric-label">{label}</div><div className="metric-value" style={{ fontSize: value.length > 18 ? 16 : 25 }}>{value}</div></div>;
}

function formatMeasurementValue(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toFixed(2) : String(value);
}

function MeasurementsPanel({ user, projectId, version, onSaved }: { user: User; projectId: string; version: MeasurementVersion | null; onSaved: () => Promise<void> }) {
  const [values, setValues] = useState<Record<string, Quantity>>(() => version?.values ?? {});
  const [waste, setWaste] = useState(() => version?.sourceWastePercentage ?? "");
  const [pitch, setPitch] = useState(() => version?.predominantPitchNotation ?? "");
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);
  const [focusedMeasurement, setFocusedMeasurement] = useState<string | null>(null);
  const lastVersionId = useRef(version?.id);

  useEffect(() => {
    if (lastVersionId.current === version?.id) return;
    lastVersionId.current = version?.id;
    setValues(version?.values ?? {});
    setWaste(version?.sourceWastePercentage ?? "");
    setPitch(version?.predominantPitchNotation ?? "");
    setSaved(false);
  }, [version]);

  function editValue(key: string, value: string) {
    setSaved(false);
    setValues((current) => ({ ...current, [key]: { ...(current[key] || { state: "measured", unit: "" }), value: value || null, state: value === "" ? "not_reported" : Number(value) === 0 ? "zero" : "measured" } }));
  }

  async function save() {
    if (!version) { setError("Upload and process a report before saving reviewed measurements."); return; }
    setSaving(true); setError(""); setSaved(false);
    try {
      await apiRequest(user, `/api/projects/${encodeURIComponent(projectId)}/measurements`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ values, sourceWastePercentage: waste === "" ? null : waste, predominantPitchNotation: pitch || null, expectedCurrentVersionId: version.id }),
      });
      setSaved(true);
      await onSaved();
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Measurements could not be saved."); }
    finally { setSaving(false); }
  }

  if (!version) return <div className="notice">Measurement review is available after the PDF extraction job produces a version. Model-backed extraction requires a configured NVIDIA API key.</div>;
  const editable = ["roofAreaSqFt", ...(version.sourceFormat === "roofr" ? ["pitchedRoofAreaSqFt", "flatRoofAreaSqFt"] : ["projectedAreaSqFt", "grossAreaSqFt", "predominantPitchDegrees", "iceWaterBarrierSqFt"]), "roofPlaneCount", "eavesLf", "rakesLf", ...(version.sourceFormat === "roofr" ? ["ridgesLf"] : ["ridgeLf"]), "hipsLf", "valleysLf", ...(version.sourceFormat === "roofr" ? ["wallFlashingLf", "stepFlashingLf", "curbFlashingLf", "transitionsLf", "unspecifiedEdgesLf"] : ["overhangOffsetLf"])];
  const labels: Record<string, string> = { roofAreaSqFt: "Total roof area", pitchedRoofAreaSqFt: "Pitched roof area", flatRoofAreaSqFt: "Flat roof area", projectedAreaSqFt: "Projected footprint", grossAreaSqFt: "Gross roof area", predominantPitchDegrees: "Predominant pitch degrees", roofPlaneCount: version.sourceFormat === "roofr" ? "Roof facets" : "Reported plane count", iceWaterBarrierSqFt: "Ice and water coverage", eavesLf: "Eaves", rakesLf: "Rakes", ridgesLf: "Ridges", ridgeLf: "Ridges", hipsLf: "Hips", valleysLf: "Valleys", wallFlashingLf: "Wall flashing", stepFlashingLf: "Step flashing", curbFlashingLf: "Curb flashing", transitionsLf: "Transitions", unspecifiedEdgesLf: "Unspecified edges", overhangOffsetLf: "Overhang offset" };
  return <div className="grid" style={{ gridTemplateColumns: "minmax(0, 1.6fr) minmax(250px, .8fr)" }}>
    <section className="card panel"><div className="panel-head"><div><h2>Source measurements</h2><div className="subcopy">Original values remain in their source version. Saving creates a new reviewed version.</div></div><span className="badge orange">{version.origin === "extracted" ? "Needs review" : "Reviewed version"}</span></div>
      <div className="table-wrap"><table><thead><tr><th>Measurement</th><th>Corrected value</th><th>Original source</th><th>Unit</th></tr></thead><tbody>{editable.map((key) => { const item = values[key]; const sourceItem = version.sourceMeasurements?.[key]; const rawValue = item?.value ?? ""; const inputValue = focusedMeasurement === key ? rawValue : rawValue === "" ? "" : formatMeasurementValue(rawValue); return <tr key={key}><td>{labels[key]}</td><td><input className="input" type="number" step="0.01" value={inputValue} onFocus={() => setFocusedMeasurement(key)} onChange={(event) => editValue(key, event.target.value)} onBlur={(event) => { setFocusedMeasurement(null); const value = event.target.value; if (value.trim() !== "" && Number.isFinite(Number(value))) editValue(key, Number(value).toFixed(2)); }} aria-label={labels[key]} /></td><td className="muted">{formatMeasurementValue(sourceItem?.value)} {sourceItem?.unit ?? ""}</td><td className="muted">{item?.unit ?? "—"}</td></tr>; })}</tbody></table></div>
      <div className="field" style={{ maxWidth: 240, marginTop: 18 }}><label htmlFor="pitch">Predominant pitch notation</label><input id="pitch" className="input" value={pitch} placeholder="Not reported" onChange={(event) => { setPitch(event.target.value); setSaved(false); }} /></div>
      <div className="field" style={{ maxWidth: 240, marginTop: 12 }}><label htmlFor="waste">Source wide waste (%)</label><input id="waste" className="input" type="number" min="0" max="100" step="0.1" value={waste} placeholder="No single source value" onChange={(event) => { setWaste(event.target.value); setSaved(false); }} /><span className="muted" style={{ fontSize: 11 }}>Leave blank when the report gives separate flat and pitched waste tables.</span></div>
      {error && <div className="error">{error}</div>}{saved && <div className="notice green-notice">Reviewed measurements saved as a new version.</div>}
      <button className="button" onClick={() => void save()} disabled={saving}>{saving ? "Saving…" : "Save reviewed version"}</button>
    </section>
    <aside className="grid" style={{ alignContent: "start" }}>{version.areasByPitch?.length ? <div className="card panel"><h3>Reported areas by pitch</h3>{version.areasByPitch.map((item) => <div className="row" key={item.pitch} style={{ justifyContent: "space-between", marginTop: 10, fontSize: 12 }}><span>{item.pitch}</span><b>{item.areaSqFt} SF</b></div>)}<p className="muted" style={{ fontSize: 11, marginBottom: 0 }}>Aggregate values remain separate from flat / pitched system totals because the source reports a classification discrepancy.</p></div> : null}{version.roofPlanes?.length ? <div className="card panel"><h3>Source facets ({version.roofPlanes.length})</h3><div className="table-wrap"><table><thead><tr><th>Facet</th><th>Type</th><th>Area label</th><th>Pitch</th></tr></thead><tbody>{version.roofPlanes.map((plane) => <tr key={plane.id}><td>{plane.id}</td><td>{plane.roofType}</td><td>{plane.areaSqFt ?? "—"} SF</td><td>{plane.pitch ?? "Unassigned"}</td></tr>)}</tbody></table></div><p className="muted" style={{ fontSize: 11, marginBottom: 0 }}>These source records preserve reported labels. Page outlines and 3D connectivity are shown separately in Geometry review.</p></div> : null}<div className="card panel"><h3>Source and correction notes</h3><ul className="subcopy" style={{ paddingLeft: 17, marginBottom: 0 }}>{version.warnings.slice(0, 5).map((warning, index) => <li key={`${warning}-${index}`}>{warning}</li>)}</ul></div></aside>
  </div>;
}

type MaterialDraft = {
  name: string;
  category: string;
  description: string;
  expectedLifespan: string;
  availabilityNote: string;
  specialOrder: boolean;
  unit: string;
  pricePerUnit: string;
  coveragePerUnit: string;
  coverageUnit: string;
  packageSize: string;
  packageUnit: string;
  defaultWastePercentage: string;
};

function createMaterialDraft(defaultWaste: string): MaterialDraft {
  return {
    name: "",
    category: "Roofing",
    description: "",
    expectedLifespan: "",
    availabilityNote: "",
    specialOrder: false,
    unit: "bundle",
    pricePerUnit: "0.00",
    coveragePerUnit: "",
    coverageUnit: "sq ft",
    packageSize: "",
    packageUnit: "unit",
    defaultWastePercentage: defaultWaste,
  };
}

function MaterialsPanel({
  user,
  config,
  selectedMaterials,
  onToggleEstimateMaterial,
  onMaterialSaved,
}: {
  user: User;
  config: AppConfig | null;
  selectedMaterials: Material[];
  onToggleEstimateMaterial: (material: Material) => void;
  onMaterialSaved: (material: Material) => void;
}) {
  const [materials, setMaterials] = useState<Material[]>([]);
  const [draft, setDraft] = useState<MaterialDraft>(() => createMaterialDraft(config?.defaultWastePercentage ?? "8"));
  const [editingMaterialId, setEditingMaterialId] = useState<string | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let active = true;
    void apiRequest<Material[]>(user, "/api/materials/initialize-defaults", { method: "POST" })
      .then((rows) => {
        if (active) setMaterials(rows);
      })
      .catch((cause: unknown) => {
        if (active) setMessage(cause instanceof Error ? cause.message : "Could not load materials.");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, [user]);

  useEffect(() => {
    if (!dialogOpen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = previousOverflow; };
  }, [dialogOpen]);

  function openAddDialog() {
    setEditingMaterialId(null);
    setDraft(createMaterialDraft(config?.defaultWastePercentage ?? "8"));
    setMessage("");
    setDialogOpen(true);
  }

  function openEditDialog(material: Material) {
    setEditingMaterialId(material.id);
    setDraft({
      name: material.name,
      category: material.category,
      description: material.description ?? "",
      expectedLifespan: material.expectedLifespan ?? "",
      availabilityNote: material.availabilityNote ?? "",
      specialOrder: material.specialOrder ?? false,
      unit: material.unit,
      pricePerUnit: material.pricePerUnit,
      coveragePerUnit: material.coveragePerUnit ?? "",
      coverageUnit: material.coverageUnit ?? "sq ft",
      packageSize: material.packageSize ?? "",
      packageUnit: material.packageUnit ?? "unit",
      defaultWastePercentage: material.defaultWastePercentage ?? config?.defaultWastePercentage ?? "8",
    });
    setMessage("");
    setDialogOpen(true);
  }

  function closeDialog() {
    if (busy) return;
    setDialogOpen(false);
    setMessage("");
  }

  function updateDraft<Key extends keyof MaterialDraft>(key: Key, value: MaterialDraft[Key]) {
    setDraft((current) => ({ ...current, [key]: value }) as MaterialDraft);
  }

  async function saveMaterial(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!draft.name.trim() || !draft.category.trim() || !draft.unit.trim()) {
      setMessage("Material name, category, and purchase unit are required.");
      return;
    }
    setBusy(true);
    setMessage("");
    const existing = editingMaterialId
      ? materials.find((material) => material.id === editingMaterialId)
      : undefined;
    const payload = {
      ...draft,
      name: draft.name.trim(),
      category: draft.category.trim(),
      description: draft.description.trim() || null,
      expectedLifespan: draft.expectedLifespan.trim() || null,
      availabilityNote: draft.availabilityNote.trim() || null,
      unit: draft.unit.trim(),
      pricePerUnit: draft.pricePerUnit || "0.00",
      currency: config?.currency || "CAD",
      coveragePerUnit: draft.coveragePerUnit || null,
      coverageUnit: draft.coveragePerUnit ? draft.coverageUnit.trim() || null : null,
      packageSize: draft.packageSize || null,
      packageUnit: draft.packageSize ? draft.packageUnit.trim() || null : null,
      defaultWastePercentage: draft.defaultWastePercentage || config?.defaultWastePercentage || "8",
      active: existing?.active ?? true,
      placeholderPrice: existing?.placeholderPrice ?? true,
    };

    try {
      const saved = await apiRequest<Material>(
        user,
        editingMaterialId
          ? "/api/materials/" + encodeURIComponent(editingMaterialId)
          : "/api/materials",
        {
          method: editingMaterialId ? "PATCH" : "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        },
      );
      setMaterials((current) => editingMaterialId
        ? current.map((material) => material.id === saved.id ? saved : material)
        : [...current, saved]);
      onMaterialSaved(saved);
      setDialogOpen(false);
      setMessage("");
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : "Catalog item could not be saved.");
    } finally {
      setBusy(false);
    }
  }

  const currency = config?.currency || "CAD";

  return <div className="grid">
    <section className="card panel">
      <div className="panel-head catalog-panel-head">
        <div>
          <h2>Material catalogue</h2>
          <div className="subcopy">Edit material details and supplier pricing. Use + to include only chosen materials in the quote estimate.</div>
        </div>
        <div className="row" style={{ gap: 8 }}>
          {selectedMaterials.length > 0 && <span className="badge">{selectedMaterials.length} selected for estimate</span>}
          <button className="button" type="button" onClick={openAddDialog}>Add material</button>
        </div>
      </div>
      {message && !dialogOpen && <div className="error" role="alert">{message}</div>}
      {loading ? <div className="empty"><p className="muted">Loading material catalogue…</p></div>
        : materials.length ? <div className="table-wrap">
          <table className="catalog-table">
            <thead><tr><th>Material name</th><th>Price ({currency})</th><th aria-label="Actions"></th></tr></thead>
            <tbody>{materials.map((material) => <tr key={material.id}>
              <td>
                <strong>{material.name}</strong>
                <div className="muted" style={{ marginTop: 3, fontSize: 11 }}>
                  {material.category} · {material.unit}
                  {material.specialOrder ? " · Special order" : ""}
                </div>
              </td>
              <td>
                <strong>{new Intl.NumberFormat("en-CA", { style: "currency", currency: material.currency || currency }).format(Number(material.pricePerUnit || 0))}</strong>
                {material.placeholderPrice !== false && <div className="muted" style={{ marginTop: 3, fontSize: 10 }}>Placeholder price</div>}
              </td>
              <td style={{ textAlign: "right" }}>
                <div className="row" style={{ justifyContent: "end", gap: 6, flexWrap: "nowrap" }}>
                  {(() => {
                    const included = selectedMaterials.some((item) => item.id === material.id);
                    return <button
                      className={`button small ${included ? "" : "secondary"}`}
                      type="button"
                      aria-pressed={included}
                      aria-label={`${included ? "Remove" : "Add"} ${material.name} ${included ? "from" : "to"} quote estimate`}
                      onClick={() => onToggleEstimateMaterial(material)}
                      title={included ? "Remove from quote estimate" : "Add to quote estimate"}
                    >
                      {included ? <Check size={14} /> : <Plus size={14} />}
                      {included ? "Added" : "Add"}
                    </button>;
                  })()}
                  <button className="button secondary small" type="button" onClick={() => openEditDialog(material)} aria-label={"Edit " + material.name}>Edit</button>
                </div>
              </td>
            </tr>)}</tbody>
          </table>
        </div> : <div className="empty"><p className="muted">No saved catalogue items yet. Add a material to get started.</p></div>}
    </section>

    {dialogOpen && typeof document !== "undefined" && createPortal(
      <div
        className="catalog-dialog-backdrop"
        onMouseDown={(event) => {
          if (event.target === event.currentTarget) closeDialog();
        }}
      >
        <section
          className="card catalog-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="catalog-dialog-title"
          onKeyDown={(event) => {
            if (event.key === "Escape") closeDialog();
          }}
        >
          <div className="catalog-dialog-header">
            <div>
              <h2 id="catalog-dialog-title">{editingMaterialId ? "Edit material" : "Add material"}</h2>
              <p className="subcopy">Enter the product details and pricing information.</p>
            </div>
            <button className="button ghost small" type="button" onClick={closeDialog} disabled={busy} aria-label="Close dialog">✕</button>
          </div>
          <form onSubmit={saveMaterial}>
            <div className="catalog-form-grid">
              <div className="field"><label htmlFor="catalog-name">Material name</label><input id="catalog-name" className="input" value={draft.name} onChange={(event) => updateDraft("name", event.target.value)} required autoFocus /></div>
              <div className="field"><label htmlFor="catalog-category">Category</label><input id="catalog-category" className="input" value={draft.category} onChange={(event) => updateDraft("category", event.target.value)} required /></div>
              <div className="field"><label htmlFor="catalog-unit">Purchase unit</label><input id="catalog-unit" className="input" value={draft.unit} onChange={(event) => updateDraft("unit", event.target.value)} required /></div>
              <div className="field"><label htmlFor="catalog-price">Unit price ({currency})</label><input id="catalog-price" className="input" type="number" min="0" step="0.01" value={draft.pricePerUnit} onChange={(event) => updateDraft("pricePerUnit", event.target.value)} required /></div>
              <div className="field catalog-field-full"><label htmlFor="catalog-description">Description</label><textarea id="catalog-description" className="input" rows={4} maxLength={3000} value={draft.description} onChange={(event) => updateDraft("description", event.target.value)} /></div>
              <div className="field"><label htmlFor="catalog-lifespan">Estimated lifespan</label><input id="catalog-lifespan" className="input" maxLength={80} value={draft.expectedLifespan} onChange={(event) => updateDraft("expectedLifespan", event.target.value)} placeholder="Optional" /></div>
              <div className="field"><label htmlFor="catalog-availability">Availability note</label><textarea id="catalog-availability" className="input" rows={2} maxLength={500} value={draft.availabilityNote} onChange={(event) => updateDraft("availabilityNote", event.target.value)} placeholder="Optional" /></div>
              <div className="field"><label htmlFor="catalog-coverage">Coverage per purchase unit</label><input id="catalog-coverage" className="input" type="number" min="0" step="any" value={draft.coveragePerUnit} onChange={(event) => updateDraft("coveragePerUnit", event.target.value)} placeholder="Optional" /></div>
              <div className="field"><label htmlFor="catalog-coverage-unit">Coverage unit</label><input id="catalog-coverage-unit" className="input" value={draft.coverageUnit} onChange={(event) => updateDraft("coverageUnit", event.target.value)} placeholder="sq ft" /></div>
              <div className="field"><label htmlFor="catalog-package-size">Units per package</label><input id="catalog-package-size" className="input" type="number" min="0" step="any" value={draft.packageSize} onChange={(event) => updateDraft("packageSize", event.target.value)} placeholder="Optional" /></div>
              <div className="field"><label htmlFor="catalog-package-unit">Package unit</label><input id="catalog-package-unit" className="input" value={draft.packageUnit} onChange={(event) => updateDraft("packageUnit", event.target.value)} placeholder="unit" /></div>
              <div className="field"><label htmlFor="catalog-waste">Default waste percentage</label><input id="catalog-waste" className="input" type="number" min="0" max="100" step="0.1" value={draft.defaultWastePercentage} onChange={(event) => updateDraft("defaultWastePercentage", event.target.value)} /></div>
              <label className="row catalog-field-full" style={{ justifyContent: "flex-start", marginBottom: 14, fontSize: 12 }}>
                <input type="checkbox" checked={draft.specialOrder} onChange={(event) => updateDraft("specialOrder", event.target.checked)} /> Special order
              </label>
            </div>
            {message && <div className="error" role="alert">{message}</div>}
            <div className="catalog-dialog-actions">
              <button className="button secondary" type="button" onClick={closeDialog} disabled={busy}>Cancel</button>
              <button className="button" type="submit" disabled={busy}>{busy ? "Saving…" : editingMaterialId ? "Save changes" : "Add material"}</button>
            </div>
          </form>
        </section>
      </div>,
      document.body,
    )}
  </div>;
}
function formatMaterialQuantity(value: number | null): string {
  return value === null || !Number.isFinite(value)
    ? "—"
    : value.toLocaleString("en-CA", { maximumFractionDigits: 2 });
}

type WasteRoofRow = {
  group: "main" | "flat" | "pitched";
  label: string;
  areaSqFt: string | null;
  roofType?: "flat" | "pitched";
};

function EstimatePanel({ user, projectId, values, version, config, selectedMaterials }: { user: User; projectId: string; values?: Record<string, Quantity>; version: MeasurementVersion | null; config: AppConfig | null; selectedMaterials: Material[] }) {
  const roofRows: WasteRoofRow[] = (() => {
    if (version?.sourceFormat === "roofr") {
      const pitchedSystem = version.roofSystems?.find((system) => system.roofType === "pitched");
      const flatSystem = version.roofSystems?.find((system) => system.roofType === "flat");
      const pitchedArea = pitchedSystem?.areaSqFt ?? version.pitchedRoofAreaSqFt ?? values?.pitchedRoofAreaSqFt?.value ?? null;
      const flatArea = flatSystem?.areaSqFt ?? version.flatRoofAreaSqFt ?? values?.flatRoofAreaSqFt?.value ?? null;
      const candidates: WasteRoofRow[] = [
        { group: "pitched", label: "Pitched roof", areaSqFt: pitchedArea, roofType: "pitched" },
        { group: "flat", label: "Flat roof", areaSqFt: flatArea, roofType: "flat" },
      ];
      const reportedSystems = new Set((version.roofSystems ?? []).map((system) => system.roofType));
      const presentSystems = candidates.filter((row) => {
        if (row.areaSqFt === null || row.areaSqFt.trim() === "") return row.roofType ? reportedSystems.has(row.roofType) : false;
        const area = Number(row.areaSqFt);
        return Number.isFinite(area) && area > 0;
      });
      if (presentSystems.length) return presentSystems;
    }
    return [{
      group: "main",
      label: "Roof total",
      areaSqFt: version?.totalRoofAreaSqFt ?? values?.roofAreaSqFt?.value ?? null,
    }];
  })();
  const measuredRoofAreaSqFt = (() => {
    const directArea = version?.totalRoofAreaSqFt ?? values?.roofAreaSqFt?.value;
    if (directArea !== undefined && directArea !== null && directArea.trim() !== "") {
      const parsed = Number(directArea);
      if (Number.isFinite(parsed) && parsed >= 0) return parsed;
    }
    const rowAreas = roofRows.map((row) => {
      if (row.areaSqFt === null || row.areaSqFt.trim() === "") return null;
      const parsed = Number(row.areaSqFt);
      return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
    });
    return rowAreas.length > 0 && rowAreas.every((area): area is number => area !== null)
      ? rowAreas.reduce((total, area) => total + area, 0)
      : null;
  })();
  const [wasteByRoof, setWasteByRoof] = useState<Record<string, string>>({});
  const [customWasteGroups, setCustomWasteGroups] = useState<Record<string, boolean>>({});
  const [savedEstimateId, setSavedEstimateId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const presetValues = ["5", "10", "15", "20"];

  function setRoofWaste(group: string, value: string, custom: boolean) {
    setSavedEstimateId(null);
    setError("");
    setCustomWasteGroups((current) => ({ ...current, [group]: custom }));
    setWasteByRoof((current) => ({ ...current, [group]: value }));
  }

  async function saveForQuote() {
    if (selectedMaterials.length > 0 && (measuredRoofAreaSqFt === null || measuredRoofAreaSqFt <= 0)) {
      setError("A measured total roof area is required to calculate the selected materials.");
      return;
    }
    if (selectedMaterials.length === 0 && roofRows.some((row) => !(wasteByRoof[row.group] ?? "").trim())) {
      setError("Choose a waste percentage for each roof, or enter a custom value.");
      return;
    }
    if (selectedMaterials.length === 0 && roofRows.some((row) => row.areaSqFt === null || row.areaSqFt.trim() === "" || !Number.isFinite(Number(row.areaSqFt)) || Number(row.areaSqFt) < 0)) {
      setError("A measured roof area is required for every row before saving.");
      return;
    }

    setSaving(true);
    setError("");
    try {
      const saved = await apiRequest<EstimateResult>(user, "/api/projects/" + encodeURIComponent(projectId) + "/estimates", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          lines: selectedMaterials.length > 0
            ? selectedMaterials.map((material) => ({
              description: material.name,
              quantity: String(measuredRoofAreaSqFt),
              quantityUnit: "sq ft",
              unit: material.unit,
              unitPrice: material.pricePerUnit,
              coveragePerUnit: material.coveragePerUnit ?? null,
              coverageUnit: material.coverageUnit ?? null,
              packageSize: material.packageSize ?? null,
              wastePercentage: wasteByRoof[material.id] ?? material.defaultWastePercentage ?? config?.defaultWastePercentage ?? "0",
              quantityOrigin: "measured",
              kind: "material",
            }))
            : roofRows.map((row) => ({
              description: row.label + " materials",
              quantity: row.areaSqFt,
              quantityUnit: "sq ft",
              unit: "sq ft",
              unitPrice: "0",
              wastePercentage: wasteByRoof[row.group],
              requireWasteSelection: true,
              quantityOrigin: "measured",
              kind: "material",
              ...(row.roofType ? { roofType: row.roofType } : {}),
            })),
          markupPercentage: "0",
          taxRate: "0",
          currency: config?.currency || "CAD",
        }),
      });
      setSavedEstimateId(saved.id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Material totals could not be saved for quote review.");
    } finally {
      setSaving(false);
    }
  }

  return <section className="card panel">
    <div className="panel-head">
      <div>
        <h2>{selectedMaterials.length > 0 ? "Selected materials for quote" : "Material waste by roof"}</h2>
        <div className="subcopy">
          {selectedMaterials.length > 0
            ? "Only the materials added from Materials & pricing will be included. Quantities use the measured roof area, catalog coverage, unit price, and each material’s waste allowance."
            : "Choose the waste allowance for each roof. Add products from Materials & pricing to include priced materials in the quote estimate."}
        </div>
      </div>
    </div>
    {selectedMaterials.length > 0 ? <>
      {measuredRoofAreaSqFt === null && <div className="notice" role="status" style={{ marginBottom: 12 }}>A measured total roof area is needed to calculate these materials.</div>}
      <div className="table-wrap"><table>
        <thead><tr><th>Material</th><th>Measured area</th><th>Waste %</th><th>Coverage per unit</th><th>Price per unit</th></tr></thead>
        <tbody>{selectedMaterials.map((material) => {
          const waste = wasteByRoof[material.id] ?? material.defaultWastePercentage ?? config?.defaultWastePercentage ?? "0";
          const wasteMode = customWasteGroups[material.id] || (waste !== "" && !presetValues.includes(waste)) ? "custom" : waste;
          return <tr key={material.id}>
            <td>
              <strong>{material.name}</strong>
              <div className="muted" style={{ marginTop: 3, fontSize: 11 }}>{material.category}</div>
              {material.placeholderPrice !== false && <div className="muted" style={{ marginTop: 3, fontSize: 10 }}>Placeholder price</div>}
            </td>
            <td>{measuredRoofAreaSqFt === null ? "Area needed" : formatMaterialQuantity(measuredRoofAreaSqFt) + " sq ft"}</td>
            <td><div className="grid" style={{ gridTemplateColumns: wasteMode === "custom" ? "minmax(105px,1fr) minmax(75px,.8fr)" : "minmax(110px,1fr)", minWidth: wasteMode === "custom" ? 195 : 115 }}>
              <select
                className="input"
                value={wasteMode}
                onChange={(event) => setRoofWaste(material.id, event.target.value === "custom" ? "" : event.target.value, event.target.value === "custom")}
                aria-label={material.name + " waste percentage"}
              >
                <option value="">Choose</option>
                <option value="5">5%</option>
                <option value="10">10%</option>
                <option value="15">15%</option>
                <option value="20">20%</option>
                <option value="custom">Custom</option>
              </select>
              {wasteMode === "custom" && <input
                className="input"
                type="number"
                min="0"
                max="100"
                step="0.1"
                value={waste}
                onChange={(event) => setRoofWaste(material.id, event.target.value, true)}
                aria-label={material.name + " custom waste percentage"}
                placeholder="%"
              />}
            </div></td>
            <td>{material.coveragePerUnit
              ? `${material.coveragePerUnit} ${material.coverageUnit || "sq ft"} / ${material.unit}`
              : <span className="muted">Set in catalogue</span>}</td>
            <td><strong>{new Intl.NumberFormat("en-CA", { style: "currency", currency: material.currency || config?.currency || "CAD" }).format(Number(material.pricePerUnit || 0))}</strong><div className="muted" style={{ fontSize: 10 }}>{material.unit}</div></td>
          </tr>;
        })}</tbody>
      </table></div>
      <p className="muted" style={{ fontSize: 11 }}>Catalog materials without coverage details may need setup before the estimate can calculate package quantities.</p>
    </> : roofRows.length === 0
      ? <div className="empty"><p className="muted">No roof areas are available for this estimate.</p></div>
      : <div className="table-wrap"><table>
        <thead><tr><th>Roof</th><th>Base material total</th><th>Waste %</th><th>Waste added</th><th>Total materials for quote</th></tr></thead>
        <tbody>{roofRows.map((row) => {
          const waste = wasteByRoof[row.group] ?? "";
          const area = row.areaSqFt !== null && row.areaSqFt.trim() !== "" && Number.isFinite(Number(row.areaSqFt))
            ? Number(row.areaSqFt)
            : null;
          const percentage = waste.trim() !== "" && Number.isFinite(Number(waste)) ? Number(waste) : null;
          const wasteAdded = area !== null && percentage !== null ? area * percentage / 100 : null;
          const totalWithWaste = area !== null && wasteAdded !== null ? area + wasteAdded : null;
          const wasteMode = customWasteGroups[row.group] || (waste !== "" && !presetValues.includes(waste)) ? "custom" : waste;
          return <tr key={row.group}>
            <td><strong>{row.label}</strong></td>
            <td>{area === null ? "Not reported" : formatMaterialQuantity(area) + " sq ft"}</td>
            <td><div className="grid" style={{ gridTemplateColumns: wasteMode === "custom" ? "minmax(110px,1fr) minmax(85px,.8fr)" : "minmax(120px,1fr)", minWidth: wasteMode === "custom" ? 220 : 130 }}>
              <select
                className="input"
                value={wasteMode}
                onChange={(event) => setRoofWaste(row.group, event.target.value === "custom" ? "" : event.target.value, event.target.value === "custom")}
                aria-label={row.label + " waste percentage"}
              >
                <option value="">Choose</option>
                <option value="5">5%</option>
                <option value="10">10%</option>
                <option value="15">15%</option>
                <option value="20">20%</option>
                <option value="custom">Custom</option>
              </select>
              {wasteMode === "custom" && <input
                className="input"
                type="number"
                min="0"
                max="100"
                step="0.1"
                value={waste}
                onChange={(event) => setRoofWaste(row.group, event.target.value, true)}
                aria-label={row.label + " custom waste percentage"}
                placeholder="%"
              />}
            </div></td>
            <td>{wasteAdded === null ? "—" : formatMaterialQuantity(wasteAdded) + " sq ft"}</td>
            <td><strong>{totalWithWaste === null ? (area === null ? "Area needed" : "Choose waste %") : formatMaterialQuantity(totalWithWaste) + " sq ft"}</strong></td>
          </tr>;
        })}</tbody>
      </table></div>}
    {(selectedMaterials.length > 0 || roofRows.length > 0) && <div className="row" style={{ justifyContent: "end", marginTop: 16 }}>
      <button className="button" type="button" onClick={() => void saveForQuote()} disabled={saving}>
        {saving ? "Saving…" : "Save material totals for quote"}
      </button>
    </div>}
    {error && <div className="error" role="alert">{error}</div>}
    {savedEstimateId && <div className="notice green-notice" role="status" style={{ marginTop: 14 }}>
      Saved for quote review · Estimate {savedEstimateId.slice(0, 12)}. {selectedMaterials.length > 0
        ? `Includes only the ${selectedMaterials.length} selected catalogue material${selectedMaterials.length === 1 ? "" : "s"}.`
        : "The selected waste percentages and adjusted material totals are attached."}
    </div>}
  </section>;
}
export function ProjectWorkspace() {
  const params = useParams<{ projectId: string }>();
  const projectId = typeof params.projectId === "string" ? params.projectId : "";
  const { user, loading } = useAuth();
  if (loading) return <div className="card empty"><p className="muted">Checking sign-in…</p></div>;
  if (!user || !firebaseConfigured) return <LoginForm />;
  return <WorkspaceContent user={user} projectId={projectId} />;
}
