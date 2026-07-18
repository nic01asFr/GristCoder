"""
survey_manifest.py — Esquisse v0.1 du Survey Manifest (strate DATA).

ESQUISSE / POINT DE DÉPART pour co-rédaction — la version canonique vivra dans
cerema-offre-de-service, à côté de scene_manifest.py (son home). Produit ici côté
artefactory-mcp pour bootstrapper, calé EXACTEMENT sur docs/survey-manifest-consumption.md §3.
Coordonné via WikiChat #survey-manifest (Formulaires-Claude, QgisSspcloud-DevOps ;
registre ComponentKind = propriété de Composants-Architect, non figé ici).

Le Survey Manifest est le CONTRAT DÉCLARATIF UNIQUE d'un questionnaire : une seule source
qui génère le formulaire (via <cerema-survey-form>), le schéma Grist (ensureSchema), le
dashboard polarity-aware et les exports. Frère (pas sous-ensemble) du Scene Manifest :
contenu distinct, mais MÊMES invariants (Classification, AuditChain, versioning INSERT-only).

Dépendances cibles (à brancher côté cerema-offre-de-service / hub/hub/models) :
    from hub.hub.models import Classification, AuditChain, ComponentProvenance
Ici on met des placeholders pour rester autonome.
"""
from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field

# ── Placeholders des invariants transverses (à remplacer par hub/hub/models) ──
Classification = Literal["cerema_internal", "public", "restricted", "confidential"]

# ── Types canoniques (miroir de docs/survey-manifest-consumption.md §3) ──────
QuestionType = Literal[
    "choice",        # Choice Grist
    "choice_list",   # ChoiceList Grist (cellule-liste ['L', v1, v2])
    "bool",          # Bool
    "likert5",       # Int 1-5 (échelle), null si non concerné
    "text",          # Text
    "datetime",      # DateTime
    "rank_place",    # matrice de classement (E1/E2/E3 : 1 lieu = 1 place)
    "geojson",       # géométrie GeoJSON (section G) -> <cerema-geo-map>, couche Scene Manifest
]

# Orientation du dashboard : fav=vert(haut=mieux) | bes=orange(haut=à traiter) | pref=bleu.
Polarity = Literal["fav", "bes", "pref", ""]

# Mapping type canonique -> type de BASE Grist attendu dans la table Reponses.
# DOIT rester identique à _SURVEY_TYPE_TO_GRIST de grist_coder.py (contrat schema-check).
TYPE_TO_GRIST: dict[str, str] = {
    "choice": "Choice", "choice_list": "ChoiceList", "bool": "Bool",
    "likert5": "Int", "text": "Text", "datetime": "DateTime",
    "rank_place": "Choice", "geojson": "Text",
}


class Question(BaseModel):
    """Une question = une colonne de la table Reponses."""
    colId: str = Field(..., description="Identifiant de colonne Grist (ex: 'H1', 'A3_Position').")
    label: str = Field(..., description="Libellé lisible affiché à l'utilisateur.")
    type: QuestionType
    theme: Optional[str] = Field(None, description="Regroupement thématique pour le dashboard.")
    polarity: Polarity = Field("", description="Oriente le top-box du dashboard.")
    choices_ref: Optional[str] = Field(None, description="Clé dans SurveyManifest.choices (choice/choice_list/rank_place).")
    scene_layer: Optional[str] = Field(None, description="type=geojson : nom de la couche Scene Manifest.")
    required: bool = False


class Section(BaseModel):
    """Une section = une étape du formulaire, éventuellement conditionnée par une gate."""
    id: str
    label: str
    gate: Optional[str] = Field(None, description="colId d'une colonne Bool ; si False -> section masquée (X0_Concerne*).")
    questions: list[Question] = Field(default_factory=list)


class SurveyManifest(BaseModel):
    """Contrat déclaratif complet d'un questionnaire (strate DATA)."""
    manifest_version: str = "1.0.0"
    id: str
    title: str
    classification: Classification = "cerema_internal"
    sections: list[Section] = Field(default_factory=list)
    choices: dict[str, list[str]] = Field(default_factory=dict, description="Jeux de valeurs réutilisables (référencés par choices_ref).")
    # Invariants transverses hérités (placeholders — à brancher sur hub/hub/models) :
    # audit: AuditChain | None = None
    # provenance: ComponentProvenance | None = None

    def expected_grist_columns(self) -> dict[str, str]:
        """{colId: type_grist_attendu} pour la table Reponses (questions + gates).
        Sert de source au check de conformité schema-check côté artefactory-mcp."""
        cols: dict[str, str] = {}
        for sec in self.sections:
            if sec.gate:
                cols[sec.gate] = "Bool"
            for q in sec.questions:
                cols[q.colId] = TYPE_TO_GRIST.get(q.type, "Text")
        return cols


# ── Exemple minimal (dérivé du questionnaire 4e Marseille) ───────────────────
EXAMPLE = SurveyManifest(
    id="perception-espaces-publics",
    title="Perception des espaces publics",
    sections=[
        Section(id="H", label="Propreté", gate="H0_Concerne", questions=[
            Question(colId="H1", label="Propreté du quartier", type="likert5", theme="Propreté", polarity="fav"),
        ]),
        Section(id="A", label="Profil", questions=[
            Question(colId="A3_Position", label="Vous êtes…", type="choice_list", choices_ref="positions"),
        ]),
        Section(id="G", label="Localisation", questions=[
            Question(colId="G_Geometries", label="Lieux cités", type="geojson", scene_layer="reponses_geo"),
        ]),
    ],
    choices={"positions": ["Habitant du quartier", "Travaille dans le quartier", "De passage"]},
)


if __name__ == "__main__":
    # Vérif rapide : le manifest -> colonnes Grist attendues.
    m = EXAMPLE
    print("manifest:", m.id, "v" + m.manifest_version, "| classification:", m.classification)
    print("colonnes Reponses attendues:", m.expected_grist_columns())
