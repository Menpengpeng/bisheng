import { Input } from "@/components/bs-ui/input";
import { Label } from "@/components/bs-ui/label";
import { Switch } from "@/components/bs-ui/switch";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/bs-ui/select";
import { LoadButton } from "@/components/bs-ui/button";
import { MultiSelect } from "@/components/bs-ui/multiSelect.tsx";
import { toast } from "@/components/bs-ui/toast/use-toast";
import { getDbTables, refreshDbSchema } from "@/controllers/API/workflow";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

export default function SqlConfigItem({ data, onChange, onValidate }) {
    const { t } = useTranslation('flow');
    const [values, setValues] = useState(data.value);
    const [errors, setErrors] = useState({});
    // F043: tables fetched at config time and related UI state
    const [fetchedTables, setFetchedTables] = useState([]);
    const [hasFetched, setHasFetched] = useState(false);
    const [tableLoading, setTableLoading] = useState(false);
    const [refreshing, setRefreshing] = useState(false);

    const {
        database_engine, db_address, db_name, db_username, db_password, open,
        selected_tables = [], schema_cache_enabled = false, schema_cache_ttl = 24,
    } = values;

    // Supported database options (DM8 uses the ?schema= connection convention)
    const DATABASE_OPTIONS = ['MySQL', 'Db2', 'PostgreSQL', 'GaussDB', 'Oracle', 'SQLServer', 'DM8'];

    // 初始化时设置默认数据库类型
    useEffect(() => {
        if (!database_engine) {
            handleChange("database_engine", "MySQL");
        }
    }, []);

    // 校验方法
    const handleValidate = () => {
        if (!open) return; // 开关关闭时无需校验

        const newErrors = {};
        const errorMessages = [];

        const validations = [
            {
                key: "database_engine",
                value: database_engine,
                requiredMsg: t("dbTypeRequired"),
            },
            {
                key: "db_address",
                value: db_address,
                max: 200,
                requiredMsg: t("dbAddressRequired"), // 数据库地址不可为空
                maxMsg: t("dbAddressTooLong"), // 数据库地址最多 200 字
            },
            {
                key: "db_name",
                value: db_name,
                max: 100,
                requiredMsg: t("dbNameRequired"), // 数据库名称不可为空
                maxMsg: t("dbNameTooLong"), // 数据库名称最多 100 字
            },
            {
                key: "db_username",
                value: db_username,
                max: 100,
                requiredMsg: t("dbUsernameRequired"), // 数据库用户名不可为空
                maxMsg: t("dbUsernameTooLong"), // 数据库用户名最多 100 字
            },
            {
                key: "db_password",
                value: db_password,
                requiredMsg: t("dbPasswordRequired"), // 数据库密码不可为空
            },
        ];

        validations.forEach(({ key, value, max, requiredMsg, maxMsg }) => {
            if (!value) {
                newErrors[key] = true;
                errorMessages.push(requiredMsg);
            } else if (max && value.length > max) {
                newErrors[key] = true;
                errorMessages.push(maxMsg);
            }
        });

        setErrors(newErrors);
        return errorMessages.length > 0 ? errorMessages[0] : false;
    };

    // 提供校验回调
    useEffect(() => {
        onValidate(handleValidate);
        return () => onValidate(() => { });
    }, [values, data.required]);

    const handleChange = (key, value) => {
        const newValues = { ...values, [key]: value };
        setValues(newValues);
        setErrors((prev) => ({ ...prev, [key]: false })); // 清除错误状态
        onChange(newValues);
    };

    // Connection params shared by both config-time requests
    const connectionParams = useMemo(() => ({
        database_engine, db_address, db_name, db_username, db_password,
    }), [database_engine, db_address, db_name, db_username, db_password]);

    // Tables still present in the database vs. selected tables that disappeared.
    // Only meaningful after a successful fetch; a persisted selection is not
    // flagged as missing before the table list has ever been loaded.
    const missingTables = useMemo(
        () => (hasFetched
            ? selected_tables.filter((name) => !fetchedTables.includes(name))
            : []),
        [hasFetched, selected_tables, fetchedTables],
    );

    // Options for the multi-select. Before the first fetch, render the
    // persisted selection as normal options so the badges show real names.
    // After fetching, missing tables come first (flagged red), then the
    // existing tables in database order.
    const tableOptions = useMemo(() => {
        if (!hasFetched) {
            return selected_tables.map((name) => ({ label: name, value: name }));
        }
        const present = fetchedTables
            .filter((name) => !missingTables.includes(name))
            .map((name) => ({ label: name, value: name }));
        const missing = missingTables.map((name) => ({
            label: t("dbTableMissingOption", { name }),
            value: name,
            error: true,
        }));
        return [...missing, ...present];
    }, [hasFetched, selected_tables, fetchedTables, missingTables, t]);

    // Fetch the visible table list for the current connection (config time)
    const handleFetchTables = async () => {
        setTableLoading(true);
        try {
            const res = await getDbTables(connectionParams);
            setFetchedTables(res?.tables || []);
            setHasFetched(true);
            // Keep the current selection untouched: selected tables that no
            // longer exist are flagged at the top instead of being dropped.
            if (res?.truncated) {
                toast({ title: t("prompt"), variant: "info", description: t("dbTableListTruncated") });
            }
        } catch (error) {
            toast({ title: t("dbFetchTablesFailed"), variant: "error", description: error || "" });
        } finally {
            setTableLoading(false);
        }
    };

    // Force-refresh the prefetched schema cache for the selected tables
    const handleRefreshCache = async () => {
        setRefreshing(true);
        try {
            const res = await refreshDbSchema({
                ...connectionParams,
                selected_tables,
                schema_cache_enabled,
                schema_cache_ttl,
            });
            const missing = res?.missing_tables || [];
            toast({
                title: t("dbRefreshCacheSuccess"),
                variant: "success",
                description: missing.length > 0 ? t("dbRefreshMissingHint", { count: missing.length }) : "",
            });
        } catch (error) {
            toast({ title: t("dbRefreshCacheFailed"), variant: "error", description: error || "" });
        } finally {
            setRefreshing(false);
        }
    };

    // Keep the raw digits while typing; clamp to 1-720 hours once the field loses focus.
    const handleTtlChange = (raw: string) => {
        if (raw === "") {
            handleChange("schema_cache_ttl", "");
            return;
        }
        if (/^\d+$/.test(raw)) {
            handleChange("schema_cache_ttl", Number(raw));
        }
    };

    const handleTtlBlur = () => {
        const n = Number(schema_cache_ttl);
        if (!Number.isFinite(n) || schema_cache_ttl === "") {
            handleChange("schema_cache_ttl", 24);
        } else {
            handleChange("schema_cache_ttl", Math.min(720, Math.max(1, n)));
        }
    };

    return (
        <div className="node-item mb-4 relative" data-key={data.key}>
            {/* 开关 */}
            <Switch
                className="absolute -top-8 right-2"
                checked={open}
                onCheckedChange={(checked) => handleChange("open", checked)}
            />

            {/* 配置表单 */}
            {open && (
                <>
                    {/* 数据库类型下拉框 */}
                    <Label className="flex items-center bisheng-label">{t("dbType")}</Label>
                    <Select
                        value={database_engine}
                        onValueChange={(value) => handleChange("database_engine", value)}
                    >
                        <SelectTrigger className={`mt-2 mb-4 nodrag ${errors['database_engine'] ? "border-red-500" : ""}`}>
                            <SelectValue placeholder={t("selectDbType")} />
                        </SelectTrigger>
                        <SelectContent>
                            {DATABASE_OPTIONS.map((engine) => (
                                <SelectItem key={engine} value={engine}>
                                    {engine}
                                </SelectItem>
                            ))}
                        </SelectContent>
                    </Select>

                    {/* 数据库地址 */}
                    <Label className="flex items-center bisheng-label">{t("dbAddress")}</Label> {/* 数据库地址 */}
                    <Input
                        className={`mt-2 nodrag ${errors['db_address'] ? "border-red-500" : ""}`}
                        value={db_address}
                        type="text"
                        onChange={(e) => handleChange("db_address", e.target.value)}
                    />

                    {/* 数据库名称 */}
                    <Label className="flex items-center bisheng-label mt-4">{t("dbName")}</Label> {/* 数据库名称 */}
                    <Input
                        className={`mt-2 nodrag ${errors['db_name'] ? "border-red-500" : ""}`}
                        value={db_name}
                        type="text"
                        onChange={(e) => handleChange("db_name", e.target.value)}
                    />

                    {/* 数据库用户名 */}
                    <Label className="flex items-center bisheng-label mt-4">{t("dbUsername")}</Label> {/* 数据库用户名 */}
                    <Input
                        className={`mt-2 nodrag ${errors['db_username'] ? "border-red-500" : ""}`}
                        value={db_username}
                        type="text"
                        onChange={(e) => handleChange("db_username", e.target.value)}
                    />

                    {/* 数据库密码 */}
                    <Label className="flex items-center bisheng-label mt-4">{t("dbPassword")}</Label> {/* 数据库密码 */}
                    <Input
                        className={`mt-2 nodrag ${errors['db_password'] ? "border-red-500" : ""}`}
                        value={db_password}
                        type="password"
                        onChange={(e) => handleChange("db_password", e.target.value)}
                    />

                    {/* F043: table selection */}
                    <Label className="flex items-center bisheng-label mt-4">{t("dbSelectedTables")}</Label>
                    <LoadButton
                        type="button"
                        variant="outline"
                        size="sm"
                        className="nodrag mt-2"
                        loading={tableLoading}
                        onClick={handleFetchTables}
                    >
                        {t("dbFetchTables")}
                    </LoadButton>
                    <MultiSelect
                        triggerClassName="nodrag mt-2"
                        options={tableOptions}
                        value={selected_tables}
                        multiple
                        searchable
                        clearable
                        placeholder={t("dbSelectTablesPlaceholder")}
                        searchPlaceholder={t("dbSearchTablesPlaceholder")}
                        emptyMessage={t("dbNoTables")}
                        onValueChange={(val) => handleChange("selected_tables", val)}
                    />
                    {missingTables.length > 0 && (
                        <p className="mt-1 text-xs text-destructive">{t("dbMissingTablesHint")}</p>
                    )}

                    {/* F043: schema cache */}
                    <div className="mt-4 flex items-center justify-between">
                        <Label className="bisheng-label">{t("dbSchemaCache")}</Label>
                        <Switch
                            className="nodrag"
                            checked={schema_cache_enabled}
                            onCheckedChange={(checked) => handleChange("schema_cache_enabled", checked)}
                        />
                    </div>
                    {schema_cache_enabled && (
                        <>
                            <div className="mt-2 flex items-center gap-2">
                                <span className="shrink-0 whitespace-nowrap text-xs text-muted-foreground">
                                    {t("dbCacheTtlLabel")}
                                </span>
                                <Input
                                    type="number"
                                    min={1}
                                    max={720}
                                    boxClassName="w-24"
                                    className="nodrag h-8 px-2"
                                    value={schema_cache_ttl}
                                    onChange={(e) => handleTtlChange(e.target.value)}
                                    onBlur={handleTtlBlur}
                                />
                                <span className="shrink-0 whitespace-nowrap text-xs text-muted-foreground">
                                    {t("dbCacheTtlUnit")}
                                </span>
                            </div>
                            <p className="mt-1 text-[11px] leading-tight text-muted-foreground">
                                {t("dbCacheTtlHint")}
                            </p>
                            <LoadButton
                                type="button"
                                variant="outline"
                                size="sm"
                                className="nodrag mt-2"
                                loading={refreshing}
                                disabled={selected_tables.length === 0}
                                onClick={handleRefreshCache}
                            >
                                {t("dbRefreshCache")}
                            </LoadButton>
                        </>
                    )}
                </>
            )}
        </div>
    );
}
