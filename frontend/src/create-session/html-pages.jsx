import React from "react";
import { EmptyState } from "../components/empty-state";
import { dlgWindow, dlgHeading, dlgInput, dlgPrimary, dlgCancel } from "../components/dialog-styles"
import { pageShell, formCard } from "../components/layout-styles"
import { Appheader } from "../header/header-component";
import { GenericDialogBox } from "../dialog/dialog-component";
import { AppFolderSelectComponent } from "../components/folder-select/folder-select-component";
import style from "./create-session.module.css";
import openFolderIcon from "../assets/img/open-folder.svg";
import IconPod from "@icons/IconPod";
import LightIcon from "@icons/Light";

// Single-step creation: name/folder/toggles, then Start session.
// (Keyword lists and topic models moved to post-hoc analysis.)

const contentWrap =
  "mx-auto flex w-full max-w-lg grow flex-col gap-4 px-4 py-6";
const fieldLabel = "mb-1.5 block text-sm font-semibold text-tiilt-ink";
const footerBar = "w-full flex-none border-t border-tiilt-line bg-white";
const footerRow = "mx-auto flex w-full max-w-lg gap-3 px-4 py-4";

function CreateSessionPage(props) {
  return (
    <>
      <div role="main" className="main-container">
        <Appheader
          title={props.pageTitle}
          leftText={false}
          rightText={""}
          nav={props.navigateToSessions}
        />
        <div className={pageShell}>
        <div className={formCard}>

        {props.currentMenu === "Settings" ? (
          <React.Fragment>
            <div className={contentWrap}>
              <div>
                <label htmlFor="txtName" className={fieldLabel}>
                  Session name
                </label>
                <input
                  id="txtName"
                  className={dlgInput}
                  defaultValue={props.sessionName}
                  onInput={(event) => props.setSessionName(event.target.value)}
                  maxLength="64"
                  placeholder="Untitled session"
                />
              </div>
              <div>
                <label className={fieldLabel}>Folder</label>
                <button
                  type="button"
                  onClick={() => props.openDialog("Folder", "test")}
                  className="flex h-11 w-full items-center justify-between gap-2 rounded-lg border border-tiilt-line bg-white px-3 text-left text-base text-tiilt-ink transition hover:border-tiilt"
                >
                  <span className="truncate">{props.folderPath || "Home"}</span>
                  <img src={openFolderIcon} alt="" className="h-5 w-5 flex-none" />
                </button>
              </div>
              <div>
                <label className={fieldLabel}>Live transcription</label>
                <div className="divide-y divide-tiilt-line overflow-hidden rounded-lg border border-tiilt-line">
                  {[
                    {
                      value: "crisperwhisper",
                      name: "CrisperWhisper 2.0 — verbatim, on this server",
                      desc: "Keeps fillers, stutters and false starts with pause-accurate word timing. Audio never leaves the machine; captions run a few seconds behind.",
                    },
                    {
                      value: "google-cloud-speech",
                      name: "Google Cloud",
                      desc: "Fastest word-by-word captions; audio is sent to Google and billed per minute.",
                    },
                  ].map((opt) => (
                    <button
                      key={opt.value}
                      type="button"
                      onClick={() => props.setAsr(opt.value)}
                      aria-pressed={props.asr === opt.value}
                      className={
                        "flex w-full items-start gap-3 px-3 py-2.5 text-left transition " +
                        (props.asr === opt.value
                          ? "bg-tiilt-soft"
                          : "bg-white hover:bg-tiilt-soft/40")
                      }
                    >
                      <span
                        className={
                          "mt-1 h-3.5 w-3.5 flex-none rounded-full border-2 " +
                          (props.asr === opt.value
                            ? "border-tiilt bg-tiilt"
                            : "border-tiilt-line")
                        }
                        aria-hidden="true"
                      />
                      <span>
                        <span className="block text-sm font-semibold text-tiilt-ink">
                          {opt.name}
                        </span>
                        <span className="block text-xs text-tiilt-muted">
                          {opt.desc}
                        </span>
                      </span>
                    </button>
                  ))}
                </div>
                <p className="mt-1 text-xs text-tiilt-muted">
                  Locked once the session starts.
                </p>
              </div>
              <div>
                <label className={fieldLabel}>Video</label>
                <label
                  htmlFor="chkLiveVideoAnalytics"
                  className="flex cursor-pointer items-start gap-3 rounded-lg border border-tiilt-line bg-white px-3 py-2.5"
                >
                  <input
                    id="chkLiveVideoAnalytics"
                    type="checkbox"
                    className="mt-0.5 h-4 w-4 flex-none cursor-pointer accent-tiilt"
                    checked={props.liveVideoAnalytics !== false}
                    onChange={(event) =>
                      props.setLiveVideoAnalytics(event.target.checked)
                    }
                  />
                  <span>
                    <span className="block text-sm font-semibold text-tiilt-ink">
                      Analyse video live (uses the GPU during class; off = record now, analyse after class)
                    </span>
                    <span className="block text-xs text-tiilt-muted">
                      Off: every group&apos;s video is still recorded, and the
                      attention and emotion analysis is queued automatically
                      when the session ends.
                    </span>
                  </span>
                </label>
              </div>
            </div>
            <div className={footerBar}>
              <div className={footerRow}>
                <button
                  className={dlgPrimary + " w-full"}
                  onClick={props.createSession}
                >
                  Start session
                </button>
              </div>
            </div>
            <div className="mx-auto w-full max-w-lg flex-none px-4 pb-4 text-center text-xs text-tiilt-muted">
              Keywords and topic models are chosen later, as part of
              post-hoc analysis on each pod.
            </div>
          </React.Fragment>
        ) : (
          <></>
        )}

        {props.currentMenu === "Devices" ? (
          <React.Fragment>
            <div className={contentWrap}>
              {props.devices.length === 0 ? (
                <EmptyState
                  title="No devices"
                  subtitle="No participant devices have joined yet."
                />
              ) : (
                <></>
              )}
              {props.devices.length > 0 ? (
                <ul className={style.list}>
                  {props.devices.map((device) => {
                    <li
                      className={
                        props.selectedDevices.includes(device)
                          ? style["selected-pod"]
                          : style["pod-item"]
                      }
                      onClick={() => props.deviceSelected(device)}
                    >
                      <IconPod className={style["pod-icon"]} aria-hidden="true" />
                      <div className={style["pod-text"]}>{device.name}</div>
                      <div className={style["button-container"]}>
                        <button
                          aria-label={`Blink light on ${device.name || "pod"}`}
                          aria-pressed={!!device.blinking}
                          className={
                            device.blinking
                              ? style["selected-button"]
                              : style["pod-button"]
                          }
                          onClick={(event) => props.blinkPod(event, device)}
                        >
                          <svg
                            x="0"
                            y="0"
                            width="20"
                            height="20"
                            viewBox="0 0 512 512"
                            className={style["light-svg"]}
                          >
                            <LightIcon></LightIcon>
                          </svg>
                        </button>
                      </div>
                    </li>;
                  })}
                </ul>
              ) : (
                <></>
              )}
              <button
                className={dlgCancel + " w-full"}
                onClick={props.onClickSelectAll}
              >
                Select all
              </button>
            </div>
            <div className={footerBar}>
              <div className={footerRow}>
                <button
                  className={dlgCancel + " flex-1"}
                  onClick={props.goToSettings}
                >
                  Previous
                </button>
                <button
                  className={dlgPrimary + " flex-1"}
                  onClick={props.createSession}
                >
                  Start session
                </button>
              </div>
            </div>
          </React.Fragment>
        ) : (
          <></>
        )}
        </div>
        </div>
      </div>

      <GenericDialogBox onClose={props.closeDialog} show={props.currentForm !== ""}>
        {props.currentForm === "Error" ? (
          <div className={dlgWindow}>
            <div className={dlgHeading}>Invalid Session</div>
            <div className="text-sm text-tiilt-ink">{props.displayText}</div>
            <button className={dlgCancel} onClick={props.closeDialog}>
              Close
            </button>
          </div>
        ) : (
          <></>
        )}

        {props.currentForm === "Folder" ? (
          <div className={dlgWindow}>
            <div className={dlgHeading}>Select folder</div>
            <AppFolderSelectComponent
              selectableFolders={props.folders}
              setFolderSelect={props.setFolderSelect}
              setBreadCrumbSelect={props.setBreadCrumbSelect}
            />
            {props.folderSelect ? (
              <button
                className={dlgPrimary}
                onClick={() =>
                  props.setFolderLocation(
                    props.folderSelect,
                    props.breadCrumbSelect
                  )
                }
              >
                OK
              </button>
            ) : (
              <></>
            )}
            <button className={dlgCancel} onClick={props.closeDialog}>
              Cancel
            </button>
          </div>
        ) : (
          <></>
        )}
      </GenericDialogBox>
    </>
  );
}

export { CreateSessionPage };
