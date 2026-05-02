import ponto_ocr, os

folder = r"uploads\ponto"
files = sorted(os.listdir(folder))[-4:]

for f in files:
    path = os.path.join(folder, f)
    d = ponto_ocr.ocr_image(path)
    print("===", f)
    print("NOME:", d.nome)
    print("CPF :", d.cpf)
    print("DATA:", d.data)
    print("HORA:", d.hora)
    print("NSR :", d.nsr)
    print("ERRO:", d.error)
    print("RAW :", repr(d.raw_text[:500]))
    print()
