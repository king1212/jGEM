"""Copyright (c) 2015-2016 Ken Sugino

.. module:: evaluate
    :synopsis: evaluate performance by comparing to a reference annotation

..  moduleauthor:: Ken Sugino <ken.sugino@gmail.com>

"""

# system imports
import gzip
import os
import subprocess
from collections import Counter
from operator import iadd
import logging
logging.basicConfig(level=logging.DEBUG)
LOG = logging.getLogger(__name__)
import json

# 3rd party libraries
import pandas as PD
import numpy as N
import matplotlib.pyplot as P

# library imports
from jgem import utils as UT
from jgem import gtfgffbed as GGB
from jgem import bedtools as BT
from jgem import bigwig as BW
from jgem import filenames as FN
from jgem import calccov as CC

class EvalNames(FN.FileNamesBase):
    """Filename manager for evaluation process.

    Attributes:
        sjexbase: path prefix to junction, exon files (*.sj.txt.gz and *.ex.txt.gz)
        code: assembly identifier
        outdir: output directory

    All outputs and temporary files are prefixed by **outdir/code**

    """

    def __init__(self, sjexbase, code, outdir):
        self.sjexbase = sjexbase
        self.code = code
        self.outdir = outdir
        for x in ['sj','ex','ci']:
            setattr(self, x+'path', '{0}.{1}.txt.gz'.format(sjexbase,x))

        prefix = os.path.join(outdir, code)
        super(EvalNames, self).__init__(prefix)

    def fname2(self, suffix, code2, category='temp'):
        """Generate filenames furthre prefixed by code2.

        Args:
            suffix: (str)
            code2: (str) identifier (comparison target)
            category: (str)

        Returns:
            (outdir)/(code).(code2).(suffix)

        """
        suf = '{0}.{1}'.format(code2, suffix)
        return self.fname(suf, category)

    def modelpath(self, which):
        """Returns path to junction(sj)/exon(ex)/choppedinterval(ci) file.

        Args:
            which: one of 'sj','ex','ci'

        """
        return '{0}.{1}.txt.gz'.format(self.sjexbase, which)

    def model(self, which):
        """Returns model dataframe (junction/exon/chopped intervals).

        Args:
            which: one of 'sj','ex', 'ci'

        """
        if hasattr(self, which): # cached
            return getattr(self, which)

        path = self.modelpath(which)
        if os.path.exists(path): # file exists
            df = UT.read_pandas(path)
            setattr(self, which, df)
            return df
        # file does not exists, if ci then make from ex
        if which=='ci':
            expath = self.modelpath['ex']
            if os.path.exists(expath):
                self.ci = UT.chopintervals(self.model['ex'], path)
            else:
                raise RuntimeError('file {0} does not exist'.format(expath))
        else:
            raise RuntimeError('file {0} does not exist'.format(path))
        
    def savemodel(self, which, code2=None, category='temp'):
        """Save model. If code2 is None, overwrite original, if code2 is provided,
        writes to outdir/(code).(code2).(which).txt.gz. 

        Args:
            which: 'sj','ex','ci'
            code2: 2nd identifier
            category: filename category (default 'temp')

        Returns:
            file path or None (if model is not loaded)

        """
        if hasattr(self, which):
            if code2 is None:
                path = self.modelpath(which)
            else:
                path = self.fname2('{0}.txt.gz'.format(which),code2, category=category)
            return UT.write_pandas(getattr(self, which), path, 'h')
        return None

WSDEFAULT = ['i',('5','5b'),('3','3b'),('s','sb'),'j']

class EvalMatch(object):
    """Compare two models against a genome coverage (bigwig) 
    and junction counts (sjfile).

    Usage:
        >>> en1 = EvalNames(sjexpre_to_ref, 'ref', outdir)
        >>> en2 = EvalNames(sjexpre_to_target, 'tgt', outdir)
        >>> em = EvalMatch(en1,en2,bwfile,sjfile,datacode)
        >>> figs = em.calculate(outdir)

    """
    
    abbr = {'i':'internal exons',
            '5':"5' exons",
            '5b':"5' exons (b)",
            '3':"3' exons",
            '3b':"3' exons (b)",
            's':'single exons',
            'sb':'single exons (b)',
            'j':'junctions'}

    def __init__(self, en1, en2, bigwig, sjfile, datacode, binsize=1000):
        """
        Args:
            en1: EvalNames object, reference
            en2: EvalNames object, sensitivity of this model against en1 is calculated
            bigwig: path to normalized bigwig coverage file
            sjfile: path to normalized junction counts file
            datacode: code indicating data (bigwig & sjfile)
            binsize (int): for sensitivity plot (default 1000)

        """
        self.en1 = en1
        self.en2 = en2
        self.bigwig = bigwig
        self.sjfile = sjfile
        self.datacode = datacode
        self.closest = {} # holds closest exon matched (for 5',3',single exons)
        self.stats = {'code1':en1.code, 'code2':en2.code, 'datacode':datacode, 
                      'binsize':binsize,'bigwig':bigwig, 'sjfile':sjfile}     
        self.ratios = {} # holds dataframes of cov(x) and ratio(y)
        self.binsize = binsize

    def calculate(self, np=1):
        """Calculate necessary data.

        1. for en1 and en2 calculate ecov,gcov,jcnt (prep_sjex)
        2. calculate match between en1 and en2 (find_match)
        3. calculate length ratio, detected numbers, sensitivity, etc. (calc_stats)

        """
        # calc exon, junction, gene coverage
        self.prep_sjex(self.en1, np)
        self.prep_sjex(self.en2, np)
        self.find_match()
        self.calc_stats()
        self.calc_completeness()

    def save(self):
        # [i,5,5b,3,3b,s,sb,j,glc,ecc,jcc]
        # light weight stats also usable from others ==> dict 
        #   auc, detected1, ..., sigmoid,...,maxx,avgx,avgy,...
        # ==> pickle or json
        fname1 = self.en2.fname2('stats.json',self.en1.code,category='output')
        with open(fname1,'w') as fp:
            json.dump(self.stats, fp)
        # [i,5,5b,3,3b,s,sb,j] cov(x),ratio(y) => in a dataframe
        # [glc,ecc,jcc] gcov(x), ratio(y) => in a dataframe
        # ==> put all in one four column dataframe (kind, id, x, y) 
        fname2 = self.en2.fname2('ratios.txt.gz',self.en1.code,category='output')
        for k, v in self.ratios.items():
            v['kind'] = k
        df = PD.concat(*self.ratios.values(), ignore_index=True)
        UT.write_pandas(df, fname2, 'h')

    def load(self):
        fname1 = self.en2.fname2('stats.json',self.en1.code,category='output')
        with open(fname1,'r') as fp:
            self.stats = json.load(fp)
        fname2 = self.en2.fname2('ratios.txt.gz',self.en1.code,category='output')
        df = UT.read_pandas(fname2)
        for k in df['kind'].unique():
            self.ratios[k] = df[df['kind']==k][['x','y']]

    def colname(self, x):
        return '{0}_{1}'.format(x, self.datacode)

    def colname2(self, x, code):
        return '{0}_{1}_{2}'.format(x, self.datacode, code)

    def prep_sjex(self, en, np=1):
        """ Assign ecov, gcov, jcnt """
        sj = en.model('sj')
        ex = en.model('ex')
        savesj = False
        saveex = False
        # check support
        dids = set(ex['d_id'].values)
        aids = set(ex['a_id'].values)
        idx = sj['a_id'].isin(aids) & sj['d_id'].isin(dids)
        sj = sj[idx].copy()
        en.sj = sj 
        # length
        if 'len' not in sj.columns:
            sj['len'] = sj['ed'] - sj['st']
            savesj = True
        if 'len' not in ex.columns:
            ex['len'] = ex['ed'] - ex['st']
            saveex = True
        # ecov
        ecovname = self.colname('ecov')
        if ecovname not in ex.columns:
            ecov = CC.calc_ecov(
                expath=en.modelpath('ex'), 
                cipath=en.modelpath('ci'), 
                bwpath=self.bigwig, 
                dstprefix=en.fname2('',self.datacode),  # cov is data dependent
                override=False, # override previous?
                np=np)
            ex[ecovname] = ecov.set_index('eid').ix[ex['_id'].values]['ecov'].values
            saveex = True
        # gcov, glen
        gcovname = self.colname('gcov')
        if gcovname not in ex.columns:
            gcov = CC.calc_gcov(
                expath=en.modelpath('ex'), 
                cipath=en.modelpath('ci'), 
                bwpath=self.bigwig, 
                dstprefix=en.fname2('',self.datacode), 
                override=False, # reuse covci from ecov calc
                np=np)
            tmp = gcov.set_index('_gidx').ix[ex['_gidx'].values]
            ex[gcovname] = tmp['gcov'].values
            ex['glen'] = tmp['glen'].values # glen is only dependent on model not data
            saveex = True
        # sjcnt
        ucntname = self.colname('ucnt')
        mcntname = self.colname('mcnt')
        jcntname = self.colname('jcnt')
        sjfile = self.sjfile
        if ucntname not in sj.columns:
            if sjfile.endswith('.bed') or sjfile.endswith('.bed.gz'): # no header
                dsj = UT.read_pandas(sjfile, names=['chr','st','ed','name','ucnt','strand','mcnt'])
            else: # assume txt file with header
                dsj = UT.read_pandas(sjfile) 
            # locus based matching
            dsj['locus'] = UT.calc_locus_strand(dsj)
            sj['locus'] = UT.calc_locus_strand(sj)
            l2u = UT.df2dict(dsj, 'locus', 'ucnt')
            l2m = UT.df2dict(dsj, 'locus', 'mcnt')
            sj[ucntname] = [l2u.get(x,0) for x in sj['locus']]
            sj[mcntname] = [l2m.get(x,0) for x in sj['locus']]
            sj[jcntname] = [x or y for x,y in sj[[ucntname,mcntname]].values]
            savesj = True
        if saveex:
            en.savemodel('ex',self.datacode)
        if savesj:
            en.savemodel('sj',self.datacode)

    def find_match(self):
        en1 = self.en1
        en2 = self.en2
        # write internal,3,5,se exons separately for finding match
        a = en1.fname('ex.bed.gz')
        b = en2.fname('ex.bed.gz')
        c = en1.fname2('ex.ovl.txt.gz', en2.code)
        self.e1 = e1 = en1.model('ex')
        self.e2 = e2 = en2.model('ex')
        ecovname = self.colname('ecov')
        cols = ['chr','st','ed','cat','_id',ecovname,'_gidx','len','strand']
        a = UT.write_pandas(e1[cols],a,'')
        b = UT.write_pandas(e2[cols],b,'')
        c = BT.bedtoolintersect(a,b,c,wao=True)
        ocols = cols + ['b_'+x for x in cols] + ['ovl']
        self.ov = ov = UT.read_pandas(c, names=ocols) # overlaps of exons
        
        idxchr = ov['chr']==ov['b_chr'] 
        idxstrand = ov['strand']==ov['b_strand']
        idxp = (ov['strand']=='+')&idxstrand
        idxn = (ov['strand']=='-')&idxstrand
        idxst = ov['st']==ov['b_st']
        idxed = ov['ed']==ov['b_ed']
        idxcat = ov['cat']==ov['b_cat']
        idxcov = ov[ecovname]>0 # exons with reads
        LOG.debug('='*10 + 'calculating match between {0} and {1}'.format(en1.code, en2.code))
        LOG.debug('len(ov):{0}'.format(len(ov)))
        for k in ['idxchr','idxstrand','idxp','idxn','idxst','idxed','idxcat','idxcov']:
            v = locals()[k]
            LOG.debug('#{0}:{1}'.format(k, N.sum(v)))
        
        # internal exon cat='i' and chr,st,ed,strand match
        self.ei = ei = ov[idxchr&idxstrand&idxst&idxed&idxcat&(ov['cat']=='i')].copy()
        # 5' cat='5' and chr,donor (+,ed)|(-,st) match, find closest
        self.e5 = e5 = ov[idxchr&((idxp&idxed)|(idxn&idxst))&idxcat&(ov['cat']=='5')].copy()
        # 3' cat='3' and chr,acceptor (+,st)|(-,ed) match
        self.e3 = e3 = ov[idxchr&((idxn&idxed)|(idxp&idxst))&idxcat&(ov['cat']=='3')] .copy()
        # se cat='s' and chr,
        self.es = es = ov[idxchr&(ov['cat']=='s')&idxcat].copy()

        # allow overlap to ther categories
        self.e5b = e5b = ov[idxchr&((idxp&idxed)|(idxn&idxst))&(ov['cat']=='5')].copy()
        # 3' cat='3' and chr,acceptor (+,st)|(-,ed) match
        self.e3b = e3b = ov[idxchr&((idxn&idxed)|(idxp&idxst))&(ov['cat']=='3')] .copy()
        # se cat='s' and chr,
        self.esb = esb = ov[idxchr&(ov['cat']=='s')].copy()
        
        # splice junction
        self.s1 = s1 = en1.model('sj')
        self.s2 = s2 = en2.model('sj')
        jcntname = self.colname('jcnt')
        l2c = UT.df2dict(s2, 'locus',jcntname)
        jhitname = self.colname2('jhit', en2.code)
        s1[jhitname] = [l2c.get(x,0) for x in s1['locus']] # corresponding s2 count
        self.sj= sj = s1[s1[jhitname]>0].copy() # only consider s2 count > 0
        
        # for batch processing
        self.e = {'i':ei,'5':e5,'3':e3,'s':es, 'j':sj, '5b':e5b, '3b':e3b, 'sb':esb}
        
    def _calc_binned(self,x0,y0,binsize):
        avgx,avgy,minx,maxx,cnt = UT.calc_binned(x0, y0, num=binsize, returnminmax=True)
        LOG.debug('len(avgx)={0},len(avgy)={1},len(minx)={2},len(maxx)={3}'.
            format(len(avgx),len(avgy),len(minx),len(maxx)))
        avgy1 = N.concatenate([avgy,[0]])
        delta = maxx - minx
        hight = (avgy+avgy1[1:])/2.
        if len(maxx)>0:
            auc = N.sum(delta*hight)/(maxx[0]-minx[-1])
        else:
            auc = 0.
        LOG.debug('len(x0)={0},len(y0)={1}, auc={2:.3f}'.format(len(x0),len(y0),auc))
        return auc,maxx,avgy,x0,y0

    def calc_stats(self):

        ecovname = self.colname('ecov')
        jcntname = self.colname('jcnt')
        jhitname = self.colname2('jhit', self.en2.code)

        def _findclosest(e, which):
            e['dlen'] = N.abs(e['len']-e['b_len'].astype(int))
            e['ratio'] = e['b_len'].astype(float)/e['len']
            e = e.sort_values(['_id','dlen'],ascending=True)
            f = e.groupby('_id',sort=False).first().reset_index()
            self.closest[which] = f
            return f

        def _count(dw, da1, da2, which):
            if which != 'j':
                da1 = da1[da1[ecovname]>0]
                dw = dw[dw[ecovname]>0]
                da2 = da2[da2[ecovname]>0]
            else:
                da1 = da1[da1[jcntname]>0]                
                dw = dw[dw[jcntname]>0]                
                da2 = da2[da2[jcntname]>0]
            pop = set(da1['_id'].values)
            hit = set(dw['_id'].values)
            pop2 = set(da2['_id'].values)
            #dif = pop.difference(hit)
            if len(pop)==0:
                LOG.warning('no elements in {0} for population1'.format(self.abbr[which]))
            if len(pop2)==0:
                LOG.warning('no elements in {0} for population2'.format(self.abbr[which]))
            if len(hit)==0:
                LOG.warning('no elements in {0} for match'.format(self.abbr[which]))
            np1,nh,np2=len(pop),len(hit),len(pop2)
            r1 = float(nh)/max(1,np1)
            r2 = float(nh)/max(1,np2)
            LOG.info( '[{5}] detected1:{0},\tmatched:{1},\t(detected2:{2}),\tratio:{3:.2f},\t(ratio2:{4:.2f})'.
                format(np1,nh,np2,r1,r2, which) )
            #return hit, pop, pop2
            return nh,np1,np2


        for which in ['i','5','3','s','j','5b','3b','sb']:
            LOG.debug(which+'='*10)
            cn = 'hit{0}'.format(which)
            if which != 'j':
                e1,e2 = self.e1,self.e2
                # use exons with reads
                ea1 = e1[(e1['cat']==which[0])][['_id',ecovname,'name']].copy() # all exons
                if len(which)==1:
                    ea2 = e2[(e2['cat']==which[0])]
                else: # all of exons allowed
                    ea2 = e2
                ew = self.e[which] # matched exons
                hit, pop, pop2 = _count(ew, ea1, ea2, which)
                ew2 = _findclosest(ew, which) # calculate ratio
                i2r = UT.df2dict(ew2,'_id','ratio')
                ea1[cn] = [i2r.get(x,0) for x in ea1['_id']]
                ea1 = ea1.set_index('_id')
                x = N.log2(ea1[ecovname]+1) # log coverage
                y = ea1[cn]
                ns = ea1['name']
            else:
                sa = self.s1
                hit, pop, pop2 = _count(self.e['j'], sa, self.s2, which)
                sa[cn] = [1 if x>0 else 0 for x in sa[jhitname]] # in case of NaN
                sa = sa.set_index('_id')
                x = N.log2(sa[jcntname]+1)
                y = sa[cn]
                ns = sa['name']
                
            # gen4 ecov>0, detected or not
            # if which != 'j':
            #     idx2 = x>0
            #     x2 = x[idx2].values
            #     y4 = N.array(y[idx2]>0, dtype=int)
            # else:
            #     x2 = x.values
            #     y4 = N.array(y>0, dtype=int)

            # only consider ones detected in the reference (en1)
            idx2 = x>0
            x2 = x[idx2].values
            y4 = N.array(y[idx2]>0, dtype=int) # binary detection indicator (ratio>0)

            try:
                x3,y3,xth = UT.fit_sigmoid(x2,y4,(0,5),0.99)
            except:
                xth = N.NaN
            auc4,maxx4,avgy4,x4,y4 = self._calc_binned(x2,y4,self.binsize)

            self.ratios[which] = PD.DataFrame({'x':x, 'y':y, 'name':ns})
            self.stats[which] = {'detected1':pop,
                                 'matched':hit,
                                 'detected2':pop2,
                                 'p1':float(hit)/pop,
                                 'p2':float(hit)/pop2,
                                 'auc':auc4,
                                 'maxx':maxx4,
                                 'avgy':avgy4,
                                 'xth':xth}

    # Not implemented yet:
    # (4. ELC: exon length completeness = max(ratio of exon length covered by overlapping target gene))
    # use ci overlaps  
    def calc_completeness(self):
        """Completeness measures how much of the reference gene structure is recovered.

        1. GLC: gene length completeness = max(ratio of gene length covered by overlapping target gene)
        2. ECC: exon count completeness = max(ratio of overlapping exon counts)
        3. JCC: junction count completeness = max(ratio of overlapping junction counts)

        """
        ov = self.ov # all
        ov2 = ov[(ov['b__gidx']!='.')&((ov['strand']==ov['b_strand'])|(ov['b_strand']=='.'))] # actual overlap with correct strand
        gcovname = self.colname('gcov')
        g2gcov = UT.df2dict(self.e1, '_gidx', gcovname)
        xlim = [0,6]
        # GLC
        g1 = ov.groupby('_gidx')
        glc = (g1['ed'].max()-g1['st'].min()).to_frame('glen')
        g2 = ov2.groupby(['_gidx','b__gidx'])
        gl2 = (g2['ed'].max()-g2['st'].min()).to_frame('b_glen').reset_index()
        gl2 = gl2.groupby('_gidx')['b_glen'].max()
        g2gl2 = UT.series2dict(gl2)
        glc['b_glen'] = [g2gl2.get(x,0) for x in glc.index]
        glc['y'] = glc['b_glen']/glc['glen']
        glc['x'] = N.log2(N.array([g2gcov[x] for x in glc.index])+1.)
        self.ratios['glc'] = glc[['x','y']]
        x,y = glc['x'].values,glc['y'].values
        x2,y2,xth = UT.fit_sigmoid(x,y,xlim,0.99)
        auc,maxx,avgy,x,y = self._calc_binned(x,y,self.binsize)
        self.stats['glc'] = {'p1':N.sum(glc['b_glen']>0)/float(len(glc)),
                             'auc':auc,'maxx':maxx,'avgy':avgy,'xth':xth}
        

        # ECC
        ecc = ov.groupby(['_gidx','_id']).first().reset_index().groupby('_gidx').size().to_frame('#exons')
        ec2 = ov2.groupby(['_gidx','b__gidx','_id']).first().reset_index()
        ec2 = ec2.groupby(['_gidx','b__gidx']).size().to_frame('ec').reset_index()
        ec2 = ec2.groupby('_gidx')['ec'].max()
        g2ec2 = UT.series2dict(ec2)
        ecc['b_#exons'] = [g2ec2.get(x,0) for x in ecc.index]
        ecc['y'] = ecc['b_#exons']/ecc['#exons']
        ecc['x'] = N.log2(N.array([g2gcov[x] for x in ecc.index])+1.)
        self.ratios['ecc'] = ecc[['x','y']]
        x,y = ecc['x'].values,ecc['y'].values
        x2,y2,xth = UT.fit_sigmoid(x,y,xlim,0.99)
        auc,maxx,avgy,x,y = self._calc_binned(x,y,self.binsize)
        self.stats['ecc'] = {'p1':N.sum(ecc['b_#exons']>0)/float(len(ecc)),
                             'auc':auc,'maxx':maxx,'avgy':avgy,'xth':xth}
                             
        # JCC
        s1 = self.s1
        jcc = s1.groupby('_gidx').size().to_frame('jc')
        l2g2 = UT.df2dict(self.s2, 'locus', '_gidx')
        s1['b__gidx'] = [l2g2.get(x,'.') for x in s1['locus'].values]
        s1o = s1[s1['b__gidx']!='.'] # overlapping
        jc2 = s1o.groupby(['_gidx','b__gidx']).size().to_frame('jc2').reset_index()
        jc2 = jc2.groupby('_gidx')['jc2'].max()
        g2jc2 = UT.series2dict(jc2)
        jcc['b_jc'] = [g2jc2.get(x,0) for x in jcc.index]
        jcc['y'] = jcc['b_jc']/jcc['jc']
        jcc['x'] = [g2gcov[x] for x in jcc.index]
        self.ratios['jcc'] = jcc[['x','y']]
        x,y = jcc['x'].values,jcc['y'].values
        x2,y2,xth = UT.fit_sigmoid(x,y,xlim,0.99)
        auc,maxx,avgy,x,y = self._calc_binned(x,y,self.binsize)
        self.stats['jcc'] = {'p1':N.sum(jcc['b_jc']>0)/float(len(jcc)),
                             'auc':auc,'maxx':maxx,'avgy':avgy,'xth':xth}

    def _plot(self, x, y, ax, ca='go-', cf='r.-', cd='b.',pw='dfat',
        binsize=25,xlim=(0,7),yth=0.99,scale=100,label='', which=None):
        """Plot dots or sigmoid fit or binned average.

        Args:
            x,y: data points, y should be in the range [0,1]
            scale: scale factor for y, default 100, i.e. [0,1]=>[0,100]
            ax: Axes object
            pw: code to indicate what to plot d:dot, f:sigmoid fit, 
              a:binned average, t:sigmoid threshold, default 'daft'
            cd: color for dot
            cf: color for sigmoid fit
            ca: color for binned average
            binsize: for binned average
            xlim: x xlimit, default (0,7)
            yth: Y threshold for sigmoid fit, xth is calculated and indicated (if 't' in pw)

        """
        if 'f' in pw or ('t' in pw and which is None):
            x2,y2,xth = UT.fit_sigmoid(x,y,xlim,yth)
        if 'd' in pw: # dot
            ax.plot(x,scale*y,'b.', alpha=0.3, label=label)
        if 'f' in pw: # fit
            ax.plot(x2,scale*y2,cf, label=label)
        if 'a' in pw: # avg
            if which is None:
                auc,maxx,avgy,x,y = self._calc_binned(x,y,self.binsize)
                #avgx,avgy = UT.calc_binned(x,y,num=binsize)
            else:
                st = self.stats[which]
                maxx,avgy = st['maxx'],st['avgy']
            ax.plot(maxx,scale*avgy, ca, label=label)
        if 't' in pw: # threshold
            if which is not None:
                xth = self.stats[which]['xth']
            ax.plot([xth,xth],[0,scale],cf+'-')
            ax.text(xth, 10, '{0:.2f}'.format(xth))
        ax.set_xlim([-0.5,xlim[1]])
        ax.set_ylim([-5,105])

    def get_detection_percentages(self):
        """Make a dataframe containing detection percentages. """
        st = self.stats
        order = ['i','5','5b','3','3b','s','sb','j']#,'glc','ecc','jcc']
        dp1 = {k: 100.*st[k]['p1'] for k in order}
        dp2 = {k: 100.*st[k]['p2'] for k in order}
        df = PD.DataFrame({'%detected 1':dp1, '%detected 2':dp2})
        return df.ix[order]

    def plot_detection(self, ax=None, w1=['i','5','3','s','j'],w2=[0]):
        """Make bar graphs of detection percentages.

        Returns:
            Axes object
        """
        if ax is None:
            fig, ax = P.subplots(1,1,figsize=(3,3))
        df = self.get_detection_percentages()
        w2 = [df.columns[x] for x in w2]
        ax = df.ix[w1][w2].plot(kind='bar', legend=False, ax=ax)
        st = self.stats
        ax.set_title('{0}/{1}'.format(st['code1'],st['code2']))

    def plot_sensitivity(self, color='b.-', ypos=0, xpos=0, axr=None, lineonly=False, ws = WSDEFAULT):
        st = self.stats
        p1c = st['code1'] # gen4
        p2c = st['code2']

        def _plot_one(ax, which, label, color, ypos=0, xpos=0):
            s = self.stats[which]
            x = N.concatenate([s['maxx'],[0]])
            y = N.concatenate([100*s['avgy'],[0]])
            # ax.plot(s['maxx'],100*s['avgy'],color+'.-',ms=5, label=label)
            ax.plot(x,y,color,ms=5, label=label)
            ma = N.ceil(N.max(s['maxx']))+0.5
            ax.set_xlim([-0.5,ma])
            ax.text(0.25+0.35*xpos,0.07*(1+ypos),'{0}: {1:.2f}'.format(label,s['auc']),
                transform=ax.transAxes)

        if axr is None:
            fig,axr = P.subplots(1,len(ws),figsize=(3*len(ws),3),sharey=True)
            P.subplots_adjust(wspace=0.07, top=0.85)
        else:
            assert len(axr)==len(ws)
            fig = None

        for i,w in enumerate(ws):
            ax = axr[i]
            if isinstance(w, tuple):
                _plot_one(ax, w[0], p2c, color, ypos, 0)
                _plot_one(ax, w[1], '- -', color+'-', ypos, 1)
                w = w[0]
            else:
                _plot_one(ax, w, p2c, color, ypos)
            if not lineonly:
                ax.set_title(self.abbr[w])
                if w!='j':
                    ax.set_xlabel('log2({0}.{1}_ecov+1)'.format(p1c, self.datacode))
                else:
                    ax.set_xlabel('log2({0}.{1}_jcnt+1)'.format(p1c, self.datacode))
        if not lineonly:
            axr[0].set_ylim([-5,105])
            axr[0].set_ylabel('%detected')
            axr[len(ws)-1].legend(loc='center left', bbox_to_anchor=(1.0,0.5))

        if fig is not None:
            fig.suptitle('{1}/{0}'.format(p1c,p2c))
        return axr

    def plot_ratio(self,axr=None,plotxlabel=True,label='',disp='both'):
        """Plot length ratios of best matching exons """
        st = self.stats
        p1c = st['code1'] # gen4
        p2c = st['code2']
        tgts = ['5','3','s']
        if axr is None:
            fig,axr = P.subplots(1,len(tgts),figsize=(3*len(tgts),3),sharex=True,sharey=True)
            P.subplots_adjust(wspace=0.07,hspace=0.15,top=0.85)
        else:
            fig = None
        for i,w in enumerate(tgts):
            ax = axr[i]
            #pop,hit,dif,auc,maxx,avgy,x,y = self.stats[w]
            st = self.stats[w]
            auc,maxx,avgy= st['auc'],st['maxx'],st['avgy']
            xy = self.ratios[w]
            x = xy['x'].values
            y = xy['y'].values
            if disp!='pdf':
                ax.plot(x,y,'.',ms=3, alpha=0.3)
            #ax.plot(maxx,avgy,'ro-',ms=3,alpha=0.3)
            ax.set_yscale('log')
            if disp!='png':
                if i==0:
                    ax.set_ylabel('{1}_len/{0}_len+1'.format(p1c,p2c))
                if plotxlabel:
                    ax.set_xlabel('log2({0}.{1}_ecov+1)'.format(p1c, self.datacode))
                ax.set_title(label+self.abbr[w])
                m = 10**(N.nanmean(N.log10(y[(x>0)&(y>0)])))
                ax.text(5,10**2,'avg:{0:.2f}'.format(m))
            else:
                ax.set_yticks([])
                ax.set_xticks([])
        if fig is not None:
            fig.suptitle('{1}/{0}'.format(p1c,p2c))
        return axr
        
    def plot_completeness(self, axr=None, tgts=['glc','ecc','jcc'], pw='dft', disp='both', title=None, **kw):
        st = self.stats
        p1c = st['code1'] # gen4
        p2c = st['code2']
        if axr is None:
            fig,axr = P.subplots(1,len(tgts),figsize=(3*len(tgts),3),sharex=True,sharey=True)
            P.subplots_adjust(wspace=0.07,hspace=0.15,top=0.85)
        else:
            fig = None
        for i, w in enumerate(tgts):
            ax = axr[i]
            d = self.ratios[w]
            x = d['x'].values
            y = d['y'].values
            self._plot(x,y,ax,pw=pw,scale=100, which=w,**kw)
            if disp!='png':
                if i==0:
                    ax.set_ylabel('% covered')
                if i==1:
                    ax.set_xlabel('log2({0}.{1}_gcov+1)'.format(p1c, self.datacode))
                ax.set_title(w.upper())
            else:
                ax.set_yticks([])
                ax.set_xticks([])
            ax.locator_params(axis='x', nbins=4)
        if fig is not None:
            if title is None:
                title = '{1}/{0}'.format(p1c,p2c)
            fig.suptitle(title)
        return axr





