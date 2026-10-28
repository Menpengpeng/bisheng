import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import useLocalize from "~/hooks/useLocalize";

// Backend pushes these status codes while a node is running (e.g. SQL agent).
const STATUS_TEXT_KEYS: Record<string, string> = {
    sql_querying: "workflow_status.sql_querying",
    sql_query_done: "workflow_status.sql_query_done",
    sql_query_failed: "workflow_status.sql_query_failed",
};

export default function MessageNodeRun({ data }) {

    const t = useLocalize()
    const [elapsed, setElapsed] = useState(0)

    // Tick once per second so long-running nodes show visible progress.
    useEffect(() => {
        const timer = setInterval(() => setElapsed((prev) => prev + 1), 1000)
        return () => clearInterval(timer)
    }, [])

    const statusKey = STATUS_TEXT_KEYS[data.status]

    return <div className="py-1">
        <div className="rounded-sm">
            <div className="flex justify-between items-center px-4 py-2 cursor-pointer">
                <div className="flex items-center font-bold gap-2 text-sm">
                    {
                        <Loader2 className="text-primary animate-spin duration-300" />
                    }
                    <span>{data.message.name}</span>
                    {statusKey && <span className="font-normal text-gray-500">{t(statusKey)}</span>}
                </div>
                <span className="text-xs text-gray-400">
                    {t("workflow_status.elapsed", { seconds: elapsed })}
                </span>
            </div>
        </div>
    </div>
};
