import json

with open(r'C:\Users\Omen\.claude\projects\c--Users-Omen-Desktop-LAVAL-Github-Repositories-artefactory-mcp\050086d3-26d8-44a6-a7b1-f512d887f155\tool-results\mcp-grist-coder-grist_records-1774053922731.txt') as f:
    data = json.load(f)
obj = json.loads(data[0]['text'])

BC_CSS = """<style>
.bc-modal{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:200;display:none;align-items:center;justify-content:center}
.bc-modal.open{display:flex}
.bc-box{background:#fff;border-radius:12px;width:560px;max-height:88vh;display:flex;flex-direction:column;box-shadow:0 20px 60px rgba(0,0,0,.3);overflow:hidden}
.bc-head{padding:14px 18px;font-weight:700;font-size:15px;display:flex;justify-content:space-between;align-items:center;flex-shrink:0}
.bc-body{flex:1;overflow-y:auto;padding:16px}
.bc-foot{padding:12px 16px;border-top:1px solid #e2e8f0;display:flex;gap:8px;justify-content:flex-end;flex-shrink:0}
.bcf{margin-bottom:12px}.bcf>label{font-size:11px;font-weight:700;color:#64748b;text-transform:uppercase;display:block;margin-bottom:4px}
.bcf select,.bcf input[type=text]{width:100%;padding:8px 10px;border:1.5px solid #e2e8f0;border-radius:6px;font-size:13px}
.bpu-t{width:100%;border-collapse:collapse;font-size:12px;margin-top:4px}
.bpu-t th{background:#f8fafc;padding:5px 8px;text-align:left;color:#64748b;font-weight:600;border-bottom:2px solid #e2e8f0}
.bpu-t td{padding:5px 8px;border-bottom:1px solid #f1f5f9}
.bc-total{background:#f0f9ff;border:1px solid #bae6fd;border-radius:6px;padding:10px 14px;display:flex;gap:16px;justify-content:space-between;font-size:13px;margin-top:8px}
</style>"""

def bc_html(color):
    return (
        '<div class="bc-modal" id="bcModal">\n'
        '  <div class="bc-box">\n'
        '    <div class="bc-head" style="background:' + color + ';color:#fff">Bon de commande '
        '<button onclick="document.getElementById(\'bcModal\').classList.remove(\'open\')" '
        'style="background:none;border:none;color:#fff;font-size:20px;cursor:pointer">x</button></div>\n'
        '    <div class="bc-body">\n'
        '      <div class="bcf"><label>March\u00e9</label><select id="bcMarcheSelect" onchange="onBCMarcheChange()"></select></div>\n'
        '      <div class="bcf"><label>Objet du BC</label><input id="bcObjet" type="text" placeholder="Description..."></div>\n'
        '      <div class="bcf"><label>Imputation budg\u00e9taire</label><input id="bcImputation" type="text" placeholder="Code comptable..."></div>\n'
        '      <div class="bcf"><label>Lignes BPU</label>\n'
        '        <table class="bpu-t"><thead><tr><th></th><th>Code</th><th>D\u00e9signation</th><th>Unit\u00e9</th><th>PU HT</th><th>Qt\u00e9</th><th>Montant HT</th></tr></thead>\n'
        '        <tbody id="bcBPUBody"></tbody></table>\n'
        '        <div style="margin-top:8px"><label style="font-size:11px;color:#64748b;display:block;margin-bottom:4px">Montant forfaitaire HT (si hors BPU)</label>\n'
        '          <input type="number" id="bcMontantManuel" min="0" step="0.01" placeholder="0.00" style="width:180px;padding:6px 10px;border:1.5px solid #e2e8f0;border-radius:6px">\n'
        '        </div>\n'
        '      </div>\n'
        '      <div id="bcTotal" class="bc-total"></div>\n'
        '    </div>\n'
        '    <div class="bc-foot">\n'
        '      <button class="abtn abtn-close" onclick="document.getElementById(\'bcModal\').classList.remove(\'open\')">Annuler</button>\n'
        '      <button class="abtn abtn-create" onclick="createBC()">Emettre le BC</button>\n'
        '    </div>\n'
        '  </div>\n'
        '</div>'
    )

BC_JS = r"""
let bpuItems=[],bcMarcheId=null,bcLines=[];
function openBCModal(){
  var mList=marches.filter(function(m){return BC_TYPES.includes(m.fields.Type_marche)&&m.fields.Statut==='En cours';});
  if(!mList.length){alert('Aucun march\u00e9 actif pour ce m\u00e9tier');return;}
  bcMarcheId=mList[0].id;bcLines=[];
  var obEl=document.getElementById('bcObjet');
  if(obEl&&selId){var d=demandes.find(function(x){return x.id===selId;});if(d&&d.fields.Description)obEl.value=d.fields.Description;}
  renderBCModal();document.getElementById('bcModal').classList.add('open');
}
function renderBCModal(){
  var mList=marches.filter(function(m){return BC_TYPES.includes(m.fields.Type_marche)&&m.fields.Statut==='En cours';});
  var m=marches.find(function(x){return x.id===bcMarcheId;});
  document.getElementById('bcMarcheSelect').innerHTML=mList.map(function(x){
    return '<option value="'+x.id+'"'+(x.id===bcMarcheId?' selected':'')+'>'+(x.fields.Ref_marche||'')+' \u2014 '+(x.fields.Objet||'')+'</option>';
  }).join('');
  document.getElementById('bcImputation').value=(m&&m.fields.Code_comptable)||'';
  var bpuList=bpuItems.filter(function(b){return b.fields.Marche===bcMarcheId;});
  if(bpuList.length){
    document.getElementById('bcBPUBody').innerHTML=bpuList.map(function(b){
      var l=bcLines.find(function(x){return x.bpuId===b.id;});
      return '<tr><td><input type="checkbox" onchange="toggleBCLine('+b.id+',this.checked)"'+(l?' checked':'')+'></td>'
        +'<td>'+(b.fields.Code_article||'')+'</td><td>'+(b.fields.Designation||'')+'</td>'
        +'<td>'+(b.fields.Unite||'')+'</td><td>'+((b.fields.Prix_ht||0).toLocaleString('fr-FR'))+' \u20ac</td>'
        +'<td><input type="number" min="0" step="0.1" value="'+(l?l.qty:1)+'" style="width:62px;padding:3px 6px;border:1px solid #cbd5e1;border-radius:4px" onchange="updateBCQty('+b.id+',+this.value)"></td>'
        +'<td id="mht-'+b.id+'">'+(l?((b.fields.Prix_ht||0)*(l.qty||1)).toLocaleString('fr-FR')+'\u20ac':'')+'</td></tr>';
    }).join('');
  } else {
    document.getElementById('bcBPUBody').innerHTML='<tr><td colspan="7" style="text-align:center;color:#94a3b8;padding:10px">Forfait \u2014 saisir montant HT ci-dessous</td></tr>';
  }
  updateBCTotal();
}
function toggleBCLine(bpuId,checked){if(checked)bcLines.push({bpuId:bpuId,qty:1});else bcLines=bcLines.filter(function(l){return l.bpuId!==bpuId;});updateBCTotal();}
function updateBCQty(bpuId,qty){var l=bcLines.find(function(x){return x.bpuId===bpuId;});if(l)l.qty=qty;var b=bpuItems.find(function(x){return x.id===bpuId;});var el=document.getElementById('mht-'+bpuId);if(el&&b)el.textContent=((b.fields.Prix_ht||0)*qty).toLocaleString('fr-FR')+'\u20ac';updateBCTotal();}
function updateBCTotal(){var ht=bcLines.reduce(function(s,l){var b=bpuItems.find(function(x){return x.id===l.bpuId;});return s+(b?(b.fields.Prix_ht||0)*(l.qty||1):0);},0);var manual=+(document.getElementById('bcMontantManuel').value)||0;if(!bcLines.length)ht=manual;var ttc=ht*1.2;document.getElementById('bcTotal').innerHTML='<span>HT: <strong>'+ht.toLocaleString('fr-FR')+' \u20ac</strong></span> <span>TVA 20%: '+(ht*0.2).toLocaleString('fr-FR')+' \u20ac</span> <span style="color:#059669;font-size:14px">TTC: <strong>'+ttc.toLocaleString('fr-FR')+' \u20ac</strong></span>';}
function onBCMarcheChange(){bcMarcheId=+document.getElementById('bcMarcheSelect').value;bcLines=[];renderBCModal();}
async function createBC(){
  var objet=document.getElementById('bcObjet').value.trim();
  if(!objet){alert('Objet obligatoire');return;}
  var ht=bcLines.reduce(function(s,l){var b=bpuItems.find(function(x){return x.id===l.bpuId;});return s+(b?(b.fields.Prix_ht||0)*(l.qty||1):0);},0);
  var manual=+(document.getElementById('bcMontantManuel').value)||0;
  if(!bcLines.length)ht=manual;
  if(!ht){alert('Montant nul');return;}
  var ttc=Math.round(ht*1.2*100)/100;
  var imputation=document.getElementById('bcImputation').value;
  var numBC='BC-'+new Date().getFullYear()+'-'+String(Date.now()).slice(-5);
  var st=typeof selType!=='undefined'?selType:'demande';
  try{
    var res=await grist.docApi.applyUserActions([['BulkAddRecord','BonsCommande',[null],{
      Num_bc:[numBC],Marche:[bcMarcheId],
      Intervention:[st==='intervention'?selId||0:0],
      Demande:[st==='demande'?selId||0:0],
      Date_emission:[Math.floor(new Date().setHours(0,0,0,0)/1000)],
      Objet_bc:[objet],Montant_ht:[ht],Taux_tva:[20],Montant_ttc:[ttc],
      Statut:['Emis'],Imputation_budgetaire:[imputation]
    }]]);
    var bcId=res.retValues&&res.retValues[0]&&res.retValues[0].rowIdsChanged&&res.retValues[0].rowIdsChanged[0];
    if(bcLines.length&&bcId){
      var N=bcLines.length;
      await grist.docApi.applyUserActions([['BulkAddRecord','Lignes_BC',new Array(N).fill(null),{
        BC:bcLines.map(function(){return bcId;}),
        BPU_ref:bcLines.map(function(l){return l.bpuId;}),
        Code_article:bcLines.map(function(l){var b=bpuItems.find(function(x){return x.id===l.bpuId;});return b?b.fields.Code_article||'':'';}),
        Designation:bcLines.map(function(l){var b=bpuItems.find(function(x){return x.id===l.bpuId;});return b?b.fields.Designation||'':'';}),
        Unite:bcLines.map(function(l){var b=bpuItems.find(function(x){return x.id===l.bpuId;});return b?b.fields.Unite||'':'';}),
        Quantite:bcLines.map(function(l){return l.qty||1;}),
        Prix_unitaire_ht:bcLines.map(function(l){var b=bpuItems.find(function(x){return x.id===l.bpuId;});return b?b.fields.Prix_ht||0:0;}),
        Montant_ht:bcLines.map(function(l){var b=bpuItems.find(function(x){return x.id===l.bpuId;});return b?(b.fields.Prix_ht||0)*(l.qty||1):0;}),
        Taux_tva:bcLines.map(function(){return 20;})
      }]]);
    }
    document.getElementById('bcModal').classList.remove('open');
    alert('BC '+numBC+' emis. TTC: '+ttc.toLocaleString('fr-FR')+' EUR');
  }catch(e){alert('Erreur: '+e.message);}
}
"""

configs = {
    19: {
        'name': 'Interface Travaux',
        'bc_types': "['Travaux']",
        'head_color': '#92400e',
        'var_old': "let tab=0,demandes=[],biens=[],marches=[],ppis=[],selId=null;",
        'var_new': "let tab=0,demandes=[],biens=[],marches=[],ppis=[],selId=null;\nconst BC_TYPES=['Travaux'];",
        'btn_old': 'onclick="cloturerDemande()">✓ Clôturer</button>\n    </div>',
        'btn_new': 'onclick="cloturerDemande()">✓ Clôturer</button>\n      <button class="abtn" style="background:#dbeafe;color:#1d4ed8" onclick="openBCModal()">BC March\u00e9</button>\n    </div>',
    },
    20: {
        'name': 'Interface MOA',
        'bc_types': "['Ma\u00eetrise d\\'ouvrage','\u00c9tudes']",
        'head_color': '#4c1d95',
        'var_old': "let tab=0,demandes=[],biens=[],marches=[],ppis=[],selId=null,selType=",
        'var_new': "let tab=0,demandes=[],biens=[],marches=[],ppis=[],selId=null,selType=",
        'btn_old': "onclick=\"cloturerDemande(${id})\">✓ Clôturer</button>",
        'btn_new': "onclick=\"cloturerDemande(${id})\">✓ Clôturer</button><button class=\"abtn\" style=\"background:#dbeafe;color:#1d4ed8;margin-top:4px\" onclick=\"openBCModal()\">BC March\u00e9</button>",
    }
}

for r in obj['records']:
    if r['id'] not in (19, 20):
        continue
    code = r['fields']['Code']
    cfg = configs[r['id']]

    # 1. Add BC_TYPES const (for MOA add it separately since var_old doesn't change)
    if r['id'] == 19:
        code = code.replace(cfg['var_old'], cfg['var_new'], 1)
    else:
        # MOA - insert BC_TYPES after the let declaration
        code = code.replace(
            "let tab=0,demandes=[],biens=[],marches=[],ppis=[],selId=null,selType='demande';",
            "let tab=0,demandes=[],biens=[],marches=[],ppis=[],selId=null,selType='demande';\nconst BC_TYPES=['\u00c9tudes','Ma\u00eetrise d\\'ouvrage'];",
            1
        )

    # 2. Add bpuItems to var list
    code = code.replace(
        "let tab=0,demandes=[],biens=[],marches=[],ppis=[],selId=null;",
        "let tab=0,demandes=[],biens=[],marches=[],ppis=[],selId=null,bpuItems=[],bcMarcheId=null,bcLines=[];",
        1
    )
    code = code.replace(
        "let tab=0,demandes=[],biens=[],marches=[],ppis=[],selId=null,selType='demande';",
        "let tab=0,demandes=[],biens=[],marches=[],ppis=[],selId=null,bpuItems=[],bcMarcheId=null,bcLines=[],selType='demande';",
        1
    )

    # 3. Add BPU_Marche fetch
    code = code.replace(
        "grist.docApi.fetchTable('Marches'),grist.docApi.fetchTable('PPI_GER')])",
        "grist.docApi.fetchTable('Marches'),grist.docApi.fetchTable('PPI_GER'),grist.docApi.fetchTable('BPU_Marche')])",
        1
    )
    code = code.replace("const [d,b,m,p]=", "const [d,b,m,p,bpu]=", 1)
    code = code.replace(
        "ppis=p.id.map((id,i)=>({id,fields:Object.fromEntries(Object.entries(p).map(([k,v])=>[k,v[i]]))}));",
        "ppis=p.id.map((id,i)=>({id,fields:Object.fromEntries(Object.entries(p).map(([k,v])=>[k,v[i]]))}));\n  bpuItems=bpu.id.map((id,i)=>({id,fields:Object.fromEntries(Object.entries(bpu).map(([k,v])=>[k,v[i]]))}));",
        1
    )

    # 4. Add BC button
    if code.find(cfg['btn_old']) != -1:
        code = code.replace(cfg['btn_old'], cfg['btn_new'], 1)
        print(f"  Button patched OK for {cfg['name']}")
    else:
        print(f"  WARNING: btn_old not found for {cfg['name']}")
        idx = code.find('cloturerDemande')
        print(f"  cloturerDemande at: {idx}, context: {repr(code[idx:idx+80])}")

    # 5. Insert BC JS before </script>
    code = code.replace('init();\n</script>', 'init();\n' + BC_JS + '\n</script>', 1)

    # 6. Insert CSS + HTML before </body>
    code = code.replace('</body></html>', BC_CSS + '\n' + bc_html(cfg['head_color']) + '\n</body></html>', 1)

    outfile = r'C:\Users\Omen\Desktop\LAVAL\Github Repositories\artefactory-mcp\tmp_' + r['id'].__str__() + '.html'
    with open(outfile, 'w', encoding='utf-8') as f:
        f.write(code)
    print(f"Written {cfg['name']}: {len(code)} chars -> {outfile}")
