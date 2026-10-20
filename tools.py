import ast, operator as op, subprocess, time

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]

def get_time():
    n = time.localtime()
    return f"{JOURS[n.tm_wday]} {n.tm_mday} {MOIS[n.tm_mon - 1]} {n.tm_year}, {n.tm_hour:02d}:{n.tm_min:02d}"

OPS = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv, ast.Mod: op.mod}

def calculate(expression):
    def ev(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in OPS:
            return OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            return -ev(n.operand)
        raise ValueError("expression non autorisée")
    return ev(ast.parse(expression, mode="eval").body)

def notify(title, text):
    subprocess.run(["termux-notification", "-t", title, "-c", text])
    return "notification envoyée"

# nom: (fonction, risque)  -> "auto" = lecture seule, "confirm" = demande ton accord
TOOLS = {
    "get_time": (get_time, "auto"),
    "calculate": (calculate, "auto"),
    "notify": (notify, "confirm"),
}

S = {"type": "STRING"}
DECL = [
    {"name": "get_time", "description": "Date et heure actuelles"},
    {"name": "calculate", "description": "Calcule une expression arithmétique",
     "parameters": {"type": "OBJECT", "properties": {"expression": S}, "required": ["expression"]}},
    {"name": "notify", "description": "Envoie une notification sur le téléphone",
     "parameters": {"type": "OBJECT", "properties": {"title": S, "text": S}, "required": ["title", "text"]}},
]
