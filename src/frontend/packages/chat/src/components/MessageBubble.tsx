import { Outlined } from 'bisheng-icons';
import { useEffect, useState, type ReactNode, type Ref } from 'react';

export interface RuntimeStatus {
  /** 已本地化的状态文案 */
  text: string;
  /** 状态开始时间（毫秒）；提供时展示每秒刷新的已等待秒数 */
  since?: number;
}
/** 二开：长耗时节点（如 SQL agent）的状态提示，内部每秒刷新已等待时间 */
export function RuntimeStatusText({ status, formatElapsed }: { status: RuntimeStatus; formatElapsed?: (seconds: number) => string }) {
  const [now, setNow] = useState(() => Date.now());
  const { since } = status;
  useEffect(() => {
    if (since === undefined) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [since]);
  const elapsed = since === undefined ? undefined : formatElapsed?.(Math.max(0, Math.floor((now - since) / 1000)));
  return <span className="font-normal text-gray-500">{status.text}{elapsed ? ` · ${elapsed}` : ''}</span>;
}

export interface ReasoningDetailsProps {
  text: string;
  pending: boolean;
  stopped?: boolean;
  labels: { thinking: string; completed: string; stopped?: string };
  /** 二开：运行中节点的状态提示 */
  status?: RuntimeStatus;
  formatElapsed?: (seconds: number) => string;
}
/** Reasoning presentation extracted from the client answer message. */
export function ReasoningDetails({ text, pending, stopped = false, labels, status, formatElapsed }: ReasoningDetailsProps) {
  const [open, setOpen] = useState(true);
  if (!text) return null;
  return <div className="py-1 mb-4">
    <div className="rounded-sm border">
      <div role="button" tabIndex={0} aria-expanded={open}
        className="flex justify-between items-center px-4 py-2 cursor-pointer"
        onClick={() => setOpen(value => !value)}
        onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setOpen(value => !value); } }}>
        <div className="flex items-center font-bold gap-2 text-sm">
          {pending ? <Outlined.Loading size={16} className="text-primary duration-300 animate-spin" />
            : stopped ? <Outlined.Square size={16} className="text-text-3" /> : <Outlined.CheckCircle size={16} className="text-success" />}
          <span>{stopped ? labels.stopped : pending ? labels.thinking : labels.completed}</span>
          {pending && status?.text && <RuntimeStatusText status={status} formatElapsed={formatElapsed} />}
        </div>
        <Outlined.Down size={16} className={open ? 'rotate-180' : undefined} />
      </div>
      {open && <div className="bg-[#F5F6F8] dark:bg-[#313336] px-4 py-2 overflow-hidden text-sm">
        {text.split('\n').map((line, index) => <p className="text-md mb-1 text-muted-foreground" key={index}>{line}</p>)}
      </div>}
    </div>
  </div>;
}

export interface MessageBubbleProps {
  title?: string;
  logo?: ReactNode;
  sender?: string;
  selection?: ReactNode;
  reasoning?: ReactNode;
  children?: ReactNode;
  attachments?: ReactNode;
  footer?: ReactNode;
  status?: ReactNode;
  pending?: boolean;
  contentRef?: Ref<HTMLDivElement>;
  hideBody?: boolean;
}
/** Host actions/resources are slots; one shared message layout owns both apps. */
export function MessageBubble({ title, logo, sender, selection, reasoning, children, attachments, footer, status, pending, contentRef, hideBody }: MessageBubbleProps) {
  return <div className="bisheng-message flex w-full py-2 items-start gap-2">
    {selection}
    <div className="w-fit group max-w-[90%]">
      {reasoning}
      {!hideBody && <>
        <div className="flex gap-2 items-center">{sender ? <p className="text-gray-600 text-xs mb-1 ml-2">{sender}</p> : <p />}</div>
        <div className="min-h-8 px-4 rounded-2xl">
          <div className="flex gap-3">
            {logo}
            <div className="min-w-0">
              {title && <p className="select-none font-semibold text-base mb-1">{title}</p>}
              <div ref={contentRef}>
                {children}
                {attachments}
                {pending && <Outlined.Loading size={24} className="animate-spin text-primary" />}
              </div>
            </div>
          </div>
        </div>
      </>}
      {status}
      {footer && <div className="flex justify-between">{footer}</div>}
    </div>
  </div>;
}
