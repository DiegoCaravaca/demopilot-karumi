"""Scoring semantico hecho por Claude mirando las capturas (cacheado: no hace falta API en directo)."""
import json
P = "data/orangehrm/graph.json"
S = {  # clave = trozo del id: (wow, tags, desc, wow_why)
 "dashboard/index": (0.85, ["overview","dashboard","resumen","analytics","kpis","time","buzz"], "Vista general: tiempo trabajado, acciones pendientes, accesos rápidos y feed social", "widgets visuales, gráfica semanal y todo el producto de un vistazo"),
 "admin/viewSystemUsers": (0.35, ["admin","users","usuarios","permisos","security","roles"], "Gestión de usuarios del sistema y roles", ""),
 "pim/viewEmployeeList": (0.6, ["employees","empleados","pim","hr","rrhh","plantilla","datos"], "Base de datos de empleados con filtros y búsqueda", "tabla viva con toda la plantilla"),
 "leave/viewLeaveList": (0.65, ["leave","vacaciones","ausencias","ausencia","approvals","aprobaciones","holidays"], "Solicitudes de vacaciones y ausencias con flujo de aprobación", "flujo de aprobación en un clic"),
 "time/viewEmployeeTimesheet": (0.55, ["time","tiempo","timesheets","horas","fichaje","attendance","costes"], "Hojas de horas y control de asistencia", ""),
 "recruitment/viewCandidates": (0.75, ["recruitment","reclutamiento","hiring","contratacion","candidates","candidatos","talent","talento"], "Pipeline de candidatos y vacantes con estados", "pipeline completo de contratación con 78 candidatos"),
 "viewPersonalDetails": (0.4, ["my info","perfil","self-service","empleado","personal"], "Ficha personal del empleado (autoservicio)", ""),
 "performance/": (0.7, ["performance","rendimiento","evaluaciones","reviews","kpis","objetivos","talent","talento"], "Evaluaciones de desempeño y KPIs por empleado", "evaluaciones y KPIs: habla el idioma de dirección"),
 "directory/": (0.7, ["directory","directorio","org","organizacion","people","equipo"], "Directorio visual de la empresa con tarjetas por persona", "tarjetas visuales de 139 personas"),
 "maintenance/": (0.1, ["maintenance","gdpr","purge","compliance"], "Purga de datos (GDPR)", ""),
 "claim/": (0.5, ["claim","gastos","expenses","reembolsos","costes","finance","finanzas"], "Solicitudes de gastos y reembolsos", ""),
 "buzz/": (0.8, ["buzz","social","comunicacion","cultura","engagement","feed"], "Red social interna: posts, likes y comentarios", "feed social: lo más 'consumer' de un HR"),
 "contactDetails": (0.2, ["contacto","contact","empleado"], "Datos de contacto del empleado", ""),
 "viewEmergencyContacts": (0.15, ["emergencia","empleado"], "Contactos de emergencia", ""),
 "viewDependents": (0.15, ["dependientes","empleado"], "Personas a cargo", ""),
 "viewImmigration": (0.25, ["immigration","visados","compliance"], "Pasaportes y visados", ""),
 "viewJobDetails": (0.4, ["job","puesto","contrato","empleado","hr"], "Puesto, contrato y departamento", ""),
 "viewSalaryList": (0.6, ["salary","salario","nomina","payroll","costes","finance","finanzas"], "Componentes salariales del empleado", "salario por componentes: clave para finanzas"),
 "viewReportToDetails": (0.45, ["org","organigrama","jerarquia","managers"], "Jerarquía: a quién reporta y quién le reporta", ""),
 "viewQualifications": (0.35, ["skills","habilidades","formacion","talent","talento"], "Habilidades, formación e idiomas", ""),
 "viewMemberships": (0.15, ["memberships","asociaciones"], "Membresías profesionales", ""),
}
g = json.load(open(P))
for n in g["nodes"]:
    for k, (w, t, d, ww) in S.items():
        if k in n["id"]:
            n.update(wow=w, tags=t, desc=d, wow_why=ww)
            break
    else:
        print("sin score:", n["id"])
json.dump(g, open(P, "w"), indent=1, ensure_ascii=False)
print("ok")
