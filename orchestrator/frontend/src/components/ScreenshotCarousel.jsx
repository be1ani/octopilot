import { useCallback, useEffect, useMemo, useState } from "react";
import { FiChevronLeft, FiChevronRight, FiExternalLink, FiMaximize2, FiX } from "react-icons/fi";
import "./ScreenshotCarousel.css";

export function ScreenshotCarousel({ shots }) {
  const list = useMemo(() => (Array.isArray(shots) ? shots : []), [shots]);
  const [idx, setIdx] = useState(0);
  const [lightbox, setLightbox] = useState(false);

  useEffect(() => {
    if (idx >= list.length) setIdx(Math.max(0, list.length - 1));
  }, [list.length, idx]);

  const prev = useCallback(() => {
    if (!list.length) return;
    setIdx((i) => (i - 1 + list.length) % list.length);
  }, [list.length]);

  const next = useCallback(() => {
    if (!list.length) return;
    setIdx((i) => (i + 1) % list.length);
  }, [list.length]);

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "ArrowLeft") prev();
      else if (e.key === "ArrowRight") next();
      else if (e.key === "Escape" && lightbox) setLightbox(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [prev, next, lightbox]);

  if (!list.length) {
    return (
      <div className="carousel-empty muted">No step screenshots were captured for this run.</div>
    );
  }

  const current = list[idx] || list[0];

  return (
    <div className="carousel">
      <div className="carousel-stage">
        <button
          type="button"
          className="carousel-nav carousel-nav--prev"
          onClick={prev}
          aria-label="Previous screenshot"
          disabled={list.length <= 1}
        >
          <FiChevronLeft />
        </button>
        <button
          type="button"
          className="carousel-image-btn"
          onClick={() => setLightbox(true)}
          title="Open full size"
        >
          <img
            src={current.url}
            alt={`Step ${current.index ?? idx + 1}`}
            loading="lazy"
            className="carousel-image"
          />
          <span className="carousel-expand" aria-hidden>
            <FiMaximize2 />
          </span>
        </button>
        <button
          type="button"
          className="carousel-nav carousel-nav--next"
          onClick={next}
          aria-label="Next screenshot"
          disabled={list.length <= 1}
        >
          <FiChevronRight />
        </button>
      </div>
      <div className="carousel-meta">
        <span className="carousel-step mono">
          Step {idx + 1} / {list.length}
        </span>
        {current.page_url ? (
          <a
            className="carousel-url mono"
            href={current.page_url}
            target="_blank"
            rel="noreferrer"
            title={current.page_url}
          >
            {current.page_url} <FiExternalLink />
          </a>
        ) : null}
        {current.field ? (
          <span
            className="carousel-goal"
            title={`Captured after filling: ${current.field}`}
          >
            filled: {current.field}
          </span>
        ) : current.next_goal ? (
          <span className="carousel-goal" title={current.next_goal}>
            {current.next_goal}
          </span>
        ) : null}
      </div>
      {list.length > 1 ? (
        <div className="carousel-strip" role="tablist" aria-label="Screenshot thumbnails">
          {list.map((s, i) => (
            <button
              key={s.index ?? i}
              type="button"
              role="tab"
              aria-selected={i === idx}
              className={`carousel-thumb${i === idx ? " carousel-thumb--active" : ""}`}
              onClick={() => setIdx(i)}
              title={`Step ${i + 1}`}
            >
              <img src={s.url} alt="" loading="lazy" />
            </button>
          ))}
        </div>
      ) : null}

      {lightbox ? (
        <div
          className="carousel-lightbox"
          role="dialog"
          aria-modal="true"
          onClick={(e) => {
            if (e.target === e.currentTarget) setLightbox(false);
          }}
        >
          <button
            type="button"
            className="carousel-lightbox-close"
            onClick={() => setLightbox(false)}
            aria-label="Close"
          >
            <FiX />
          </button>
          <img src={current.url} alt={`Step ${idx + 1} full size`} />
        </div>
      ) : null}
    </div>
  );
}
